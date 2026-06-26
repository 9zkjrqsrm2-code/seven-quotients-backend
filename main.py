"""
人类七商测试 - 后端 API + 管理后台
FastAPI + SQLAlchemy + Stripe + Jinja2
"""
import os
import json
import hashlib
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from models import Base, User, Question, TestSession, Answer, Order, AdConfig, init_db

load_dotenv()

# ─── Config ──────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PAID_PRICE_USD = int(os.getenv("PAID_PRICE_USD", "499"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
SITE_URL = os.getenv("SITE_URL", "http://localhost:8000")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")

stripe.api_key = STRIPE_SECRET_KEY

# ─── Database ────────────────────────────────────────────
engine = create_engine(DATABASE_URL, echo=False)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Lifespan ────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    yield


app = FastAPI(title="七商测试 API", version="1.0", lifespan=lifespan)

# ─── Templates & Static ──────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# ═══════════════════════════════════════════════════════════
#   ADMIN 辅助
# ═══════════════════════════════════════════════════════════
def verify_admin(username: str, password: str) -> bool:
    if username != ADMIN_USERNAME:
        return False
    # 简单密码验证，生产建议用 bcrypt verify
    return hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH


def get_admin_session(request: Request) -> bool:
    return request.cookies.get("admin_token") == hashlib.sha256(
        (ADMIN_USERNAME + SECRET_KEY).encode()
    ).hexdigest()[:32]


# ═══════════════════════════════════════════════════════════
#   TEST API
# ═══════════════════════════════════════════════════════════
@app.post("/api/start")
def start_test(
    is_paid: bool = False,
    client_ip: str = Query(""),
    db: Session = Depends(get_db),
):
    """开始测试，返回 session_id 和题目列表"""
    user = User(client_ip=client_ip or "unknown")
    db.add(user)
    db.flush()

    session = TestSession(user_id=user.id, is_paid_test=is_paid)
    db.add(session)
    db.flush()

    # 取题目
    qs = db.query(Question).filter(
        Question.is_active == True,
        Question.is_paid == is_paid,
    ).order_by(Question.sort_order).all()

    # 若没有付费题目，用内置题目
    if not qs:
        qs = _get_builtin_questions(is_paid)

    questions_data = [
        {
            "id": q.id,
            "q": q.question_text,
            "qid": q.category,
            "opts": q.options,
        }
        for q in qs
    ]

    db.commit()

    return {
        "session_id": session.id,
        "user_id": user.id,
        "questions": questions_data,
        "total": len(questions_data),
    }


@app.post("/api/answer")
def submit_answer(
    session_id: str = Form(...),
    question_id: str = Form(...),
    score: int = Form(...),
    db: Session = Depends(get_db),
):
    """提交一道题的答案"""
    answer = Answer(
        session_id=session_id,
        question_id=question_id,
        score=score,
    )
    db.add(answer)
    db.commit()
    return {"ok": True}


@app.post("/api/finish")
def finish_test(
    session_id: str = Form(...),
    answers_json: str = Form(...),  # [{"question_id": "xxx", "score": 5}, ...]
    db: Session = Depends(get_db),
):
    """完成测试，计算结果"""
    session = db.query(TestSession).filter(TestSession.id == session_id).first()
    if not session:
        raise HTTPException(404, "Session not found")

    answers_data = json.loads(answers_json)
    category_scores = {c: [] for c in ["mq", "iq", "eq", "aq", "fq", "sq", "hq"]}

    for ans in answers_data:
        q = db.query(Question).filter(Question.id == ans["question_id"]).first()
        if q:
            category_scores.setdefault(q.category, []).append(ans["score"])
        # Save answer
        db.add(Answer(
            session_id=session_id,
            question_id=ans["question_id"],
            score=ans["score"],
        ))

    results = {}
    for cat, scores in category_scores.items():
        if scores:
            avg = sum(scores) / len(scores)
            results[cat] = round(avg, 1)
        else:
            results[cat] = 0

    session.completed = True
    session.results = results
    session.completed_at = datetime.now(timezone.utc)
    db.commit()

    return {"results": results}


def _get_builtin_questions(is_paid: bool):
    """内置题目（数据库为空时备用）"""
    builtin = [
        # 免费题（14道）
        ("mq", "你在路上看到一位老人摔倒，你会怎么做？",
         ["立即上前扶起并帮忙联系家人", "先拍照留存证据再帮忙", "犹豫后选择报警",
          "看看周围有没有人去帮忙", "当作没看见直接走开"]),
        ("mq", "在工作中发现同事做了一个违背职业道德的决定，你会？",
         ["立即劝阻并说明原因", "向上级反映情况", "私下提醒同事",
          "装作不知道", "觉得与自己无关"]),
        ("iq", "遇到一个复杂问题时，你通常会？",
         ["快速分析并找到多种解决方案", "花时间研究后能找到方法", "需要别人帮助才能解决",
          "会觉得非常困难无从下手", "直接放弃"]),
        ("iq", "学习新知识或新技能时，你感觉？",
         ["非常轻松，很快就能掌握", "比较顺利，稍加练习即可", "需要一定时间和努力",
          "比较吃力，需要反复学习", "非常困难，很难学会"]),
        ("eq", "当朋友心情不好的时候，你通常会？",
         ["敏锐察觉并给予恰当安慰", "能察觉到并试着安慰", "能注意到但不知道怎么做",
          "需要对方说出来才知道", "不太能察觉别人的情绪"]),
        ("eq", "在团队中与他人意见不合时，你会？",
         ["很好沟通并达成共识", "表达观点并倾听对方", "选择让步以避免冲突",
          "坚持己见不妥协", "生闷气或直接离开"]),
        ("aq", "遇到重大挫败（如失业、落榜）时，你会？",
         ["很快调整心态寻找新机会", "难过一段时间后重新振作", "需要很长时间才能走出来",
          "变得非常消沉低落", "很难接受现实一蹶不振"]),
        ("aq", "面对一个非常困难的任务，你通常会？",
         ["充满挑战欲，积极想办法", "虽觉困难但会尽力而为", "先试试看不行再放弃",
          "感到焦虑和巨大压力", "直接选择逃避"]),
        ("fq", "对于每个月的收入，你通常如何管理？",
         ["有完善的理财规划和投资", "有储蓄计划并严格执行", "会存一部分钱但无明确规划",
          "基本月光，有多少花多少", "经常入不敷出"]),
        ("fq", "对于投资理财（股票、基金等），你的态度是？",
         ["很了解并有丰富的实践经验", "有一定了解并愿意尝试", "了解不多但有兴趣学习",
          "觉得风险太大不敢尝试", "完全不感兴趣"]),
        ("sq", "你是否有明确的个人信念或人生目标？",
         ["有非常清晰的人生目标与信念", "有较为明确的方向", "在探索中有些模糊想法",
          "不太清楚自己想要什么", "从未思考过这些问题"]),
        ("sq", "当你独处时，你通常会？",
         ["享受独处进行深度思考和反省", "静下心来看书或冥想", "做些轻松的事情放松",
          "感到无聊和孤独", "必须找人陪伴"]),
        ("hq", "你的生活习惯如何？",
         ["作息规律，定期锻炼，饮食均衡", "比较注意健康，偶尔锻炼", "有时熬夜，饮食不太注意",
          "经常熬夜，很少运动", "生活完全没有规律"]),
        ("hq", "关于健康知识，你觉得自己？",
         ["非常了解并能科学管理健康", "有基本的健康知识储备", "有一定了解但不全面",
          "了解很少", "完全不关心"]),
    ]
    result = []
    for i, (cat, q, opts) in enumerate(builtin, 1):
        q_obj = Question(
            id=f"builtin_{i:02d}",
            category=cat,
            question_text=q,
            options=opts,
            scores=[5, 4, 3, 2, 1],
            sort_order=i,
            is_paid=False,
            is_active=True,
        )
        result.append(q_obj)
    return result


@app.get("/api/questions/{session_id}")
def get_questions(session_id: str, db: Session = Depends(get_db)):
    """获取已付费用户的完整题目"""
    session = db.query(TestSession).filter(TestSession.id == session_id).first()
    if not session or not session.user.paid:
        raise HTTPException(403, "请先付费解锁完整版")

    qs = db.query(Question).filter(
        Question.is_active == True,
        Question.is_paid == True,
    ).order_by(Question.sort_order).all()

    return {
        "questions": [
            {"id": q.id, "q": q.question_text, "qid": q.category, "opts": q.options}
            for q in qs
        ]
    }


@app.get("/api/ad")
def get_ad(placement: str = "result_bottom", db: Session = Depends(get_db)):
    """获取广告配置"""
    ad = db.query(AdConfig).filter(
        AdConfig.placement == placement,
        AdConfig.enabled == True,
    ).first()
    if ad:
        return {"code": ad.ad_code, "enabled": True}
    return {"code": "", "enabled": False}


# ═══════════════════════════════════════════════════════════
#   PAYMENT API
# ═══════════════════════════════════════════════════════════
@app.post("/api/payment/create-checkout")
def create_checkout(
    user_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """创建 Stripe Checkout Session（支持微信支付 + 支付宝）"""
    if not STRIPE_SECRET_KEY:
        return {"url": f"{SITE_URL}/?payment=mock&user_id={user_id}"}

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")

    try:
        session = stripe.checkout.Session.create(
            line_items=[{
                "price_data": {
                    "currency": "cny",
                    "product_data": {"name": "七商测试 - 完整版",
                                     "description": "42道深度测试题 + 详细分析报告"},
                    "unit_amount": int(os.getenv("PAID_PRICE_CNY", "2999")),
                },
                "quantity": 1,
            }],
            mode="payment",
            payment_method_types=["wechat_pay", "alipay"],
            success_url=f"{SITE_URL}/?payment=success&session_id={{CHECKOUT_SESSION_ID}}&user_id={user_id}",
            cancel_url=f"{SITE_URL}/?payment=cancel",
            metadata={"user_id": user_id},
        )

        user.stripe_session_id = session.id
        db.add(Order(
            user_id=user_id,
            stripe_session_id=session.id,
            amount=session.amount_total or int(os.getenv("PAID_PRICE_CNY", "2999")),
            currency="cny",
            status="pending",
        ))
        db.commit()

        return {"url": session.url}
    except Exception as e:
        raise HTTPException(500, f"Payment error: {str(e)}")


@app.post("/api/payment/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe Webhook 处理支付成功回调"""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        return {"ok": True}

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(400, "Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")

    if event["type"] == "checkout.session.completed":
        session_data = event["data"]["object"]
        user_id = session_data.get("metadata", {}).get("user_id", "")
        stripe_session_id = session_data.get("id", "")

        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.paid = True

        order = db.query(Order).filter(
            Order.stripe_session_id == stripe_session_id
        ).first()
        if order:
            order.status = "completed"
            order.completed_at = datetime.now(timezone.utc)
            pay_method = session_data.get("payment_method_types", ["unknown"])[0]
            order.payment_method = pay_method

        db.commit()

    return {"ok": True}


@app.get("/api/payment/verify")
def verify_payment(user_id: str, db: Session = Depends(get_db)):
    """验证用户是否已付费"""
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        return {"paid": user.paid}
    return {"paid": False}


# ═══════════════════════════════════════════════════════════
#   ADMIN PANEL
# ═══════════════════════════════════════════════════════════
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.post("/admin/login")
def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_admin(username, password):
        token = hashlib.sha256((ADMIN_USERNAME + SECRET_KEY).encode()).hexdigest()[:32]
        resp = RedirectResponse(url="/admin/dashboard", status_code=302)
        resp.set_cookie(key="admin_token", value=token, httponly=True, max_age=86400 * 7)
        return resp
    return templates.TemplateResponse("admin_login.html", {
        "request": request, "error": "用户名或密码错误"
    })


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_token")
    return resp


def admin_required(request: Request):
    if not get_admin_session(request):
        raise HTTPException(302, detail="Unauthorized",
                            headers={"Location": "/admin/login"})


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    total_users = db.query(User).count()
    paid_users = db.query(User).filter(User.paid == True).count()
    total_orders = db.query(Order).count()
    completed_orders = db.query(Order).filter(Order.status == "completed").count()
    total_revenue = sum(
        o.amount for o in db.query(Order).filter(Order.status == "completed").all()
    )
    recent_orders = db.query(Order).order_by(Order.created_at.desc()).limit(10).all()

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "total_users": total_users,
        "paid_users": paid_users,
        "total_orders": total_orders,
        "completed_orders": completed_orders,
        "total_revenue": total_revenue,
        "recent_orders": recent_orders,
    })


@app.get("/admin/questions", response_class=HTMLResponse)
def admin_questions(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    total = db.query(Question).count()
    questions = db.query(Question).order_by(
        Question.is_paid, Question.sort_order
    ).offset((page - 1) * per_page).limit(per_page).all()

    return templates.TemplateResponse("admin_questions.html", {
        "request": request,
        "questions": questions,
        "page": page,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "categories": ["mq", "iq", "eq", "aq", "fq", "sq", "hq"],
    })


@app.post("/admin/questions/add")
def add_question(
    request: Request,
    category: str = Form(...),
    question_text: str = Form(...),
    option_a: str = Form(...),
    option_b: str = Form(...),
    option_c: str = Form(...),
    option_d: str = Form(...),
    option_e: str = Form(...),
    is_paid: bool = Form(False),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    q = Question(
        category=category,
        question_text=question_text,
        options=[option_a, option_b, option_c, option_d, option_e],
        scores=[5, 4, 3, 2, 1],
        is_paid=is_paid,
        sort_order=sort_order,
    )
    db.add(q)
    db.commit()
    return RedirectResponse(url="/admin/questions", status_code=302)


@app.get("/admin/questions/{qid}/toggle")
def toggle_question(qid: str, request: Request, db: Session = Depends(get_db)):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    q = db.query(Question).filter(Question.id == qid).first()
    if q:
        q.is_active = not q.is_active
        db.commit()
    return RedirectResponse(url="/admin/questions", status_code=302)


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, page: int = 1, db: Session = Depends(get_db)):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    orders = db.query(Order).order_by(Order.created_at.desc()).offset(
        (page - 1) * 20
    ).limit(20).all()
    total = db.query(Order).count()

    return templates.TemplateResponse("admin_orders.html", {
        "request": request,
        "orders": orders,
        "page": page,
        "total_pages": max(1, (total + 19) // 20),
    })


@app.get("/admin/ads", response_class=HTMLResponse)
def admin_ads(request: Request, db: Session = Depends(get_db)):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    ads = db.query(AdConfig).all()
    if not ads:
        for p in ["result_top", "result_bottom", "sidebar"]:
            db.add(AdConfig(placement=p, ad_code="", enabled=False))
        db.commit()
        ads = db.query(AdConfig).all()

    return templates.TemplateResponse("admin_ads.html", {
        "request": request,
        "ads": ads,
    })


@app.post("/admin/ads/update")
def update_ad(
    request: Request,
    ad_id: str = Form(...),
    ad_code: str = Form(""),
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    ad = db.query(AdConfig).filter(AdConfig.id == ad_id).first()
    if ad:
        ad.ad_code = ad_code
        ad.enabled = enabled
        ad.updated_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/ads", status_code=302)


# ═══════════════════════════════════════════════════════════
#   ADMIN SEED
# ═══════════════════════════════════════════════════════════
@app.get("/admin/seed")
def seed_questions(request: Request, db: Session = Depends(get_db)):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    existing = db.query(Question).count()
    if existing > 20:
        return JSONResponse({"msg": f"数据库已有 {existing} 道题，跳过种子数据"})

    questions = _get_builtin_questions(False)
    for q in questions:
        db.add(q)

    # 付费种子题目
    paid_questions = [
        ("mq", "如果你发现朋友做了违法但不伤害他人的事，你会？",
         ["劝他自首并陪同前往", "严肃劝诫让他改正", "当作没看见但不再往来",
          "只要不牵连自己就不管", "觉得无所谓"]),
        ("mq", "在利益冲突时，你如何选择？",
         ["坚守原则，宁可吃亏", "尽量找双赢方案", "在不伤害他人的前提下争取利益",
          "利己优先但不过分", "不择手段争取利益"]),
        ("iq", "给你一个全新的概念，你通常多久能理解其核心？",
         ["几分钟内就能抓住要点", "半小时左右", "需要一两天消化",
          "需要反复学习好几遍", "很难真正理解"]),
        ("iq", "你擅长从大量信息中快速找出关键点吗？",
         ["非常擅长，几乎不会遗漏", "比较擅长，能抓住主要矛盾", "一般，需要时间整理",
          "不太擅长，容易被细节干扰", "完全不知道从何下手"]),
        ("eq", "你在压力下与人沟通的能力如何？",
         ["即使压力很大也能保持冷静沟通", "大部分情况下能控制情绪", "偶尔会情绪失控",
          "压力大时容易发脾气", "完全无法正常交流"]),
        ("eq", "你能准确理解别人没说出口的需求吗？",
         ["经常能，对方一说半句我就懂了", "大多数时候可以", "偶尔能猜到",
          "很少能理解", "完全察觉不到"]),
        ("aq", "被拒绝后你通常需要多久恢复？",
         ["几分钟就能重新开始", "几小时内调整好", "需要一两天",
          "会沮丧很久", "再也不敢尝试"]),
        ("aq", "面对不确定的情况，你的第一反应是？",
         ["兴奋，认为充满机会", "谨慎但愿意尝试", "有点不安但会面对",
          "感到焦虑想逃避", "非常恐惧不敢行动"]),
        ("fq", "你有做预算的习惯吗？",
         ["详细的月度/年度预算并追踪", "有大致预算框架", "心里有数但不做记录",
          "从来不做预算", "完全不知道自己花了多少"]),
        ("fq", "关于负债，你的态度是？",
         ["除房贷外尽量零负债", "合理利用信用卡免息期", "偶尔透支但及时还清",
          "经常透支只还最低", "负债累累无力偿还"]),
        ("sq", "你觉得人生的意义是什么？",
         ["有非常清晰的答案和践行路径", "有大致方向但还在探索", "偶尔会思考但没有定论",
          "觉得想这些没用", "从不思考人生意义"]),
        ("sq", "你每天会留多少时间给自己深度思考？",
         ["1小时以上", "30分钟到1小时", "10-30分钟",
          "偶尔有空时才会", "几乎从不独处思考"]),
        ("hq", "你每周运动多少次？",
         ["5次以上，有系统训练", "3-4次规律运动", "1-2次偶尔运动",
          "几乎不运动", "完全不动"]),
        ("hq", "你对饮食营养的关注程度？",
         ["科学搭配，注意热量和营养", "大致注意健康饮食", "凭感觉吃但不太挑",
          "想吃什么吃什么", "经常暴饮暴食"]),
        ("mq", "看到网络上的不实信息在传播，你会？",
         ["主动辟谣并附上证据", "在评论区理性指出问题", "只转发给熟人提醒",
          "看看而已不理会", "觉得与自己无关"]),
        ("iq", "你能否把复杂的事情用简单的话讲清楚？",
         ["总能深入浅出地讲明白", "大多数时候可以", "需要准备一下",
          "讲着讲着就绕进去了", "完全做不到"]),
        ("eq", "发生冲突后你主动和解的频率？",
         ["总是主动沟通化解矛盾", "大多数情况会主动", "看情况而定",
          "等着对方先开口", "从不主动和解"]),
        ("aq", "你如何看待失败？",
         ["最好的学习机会", "成长必经之路", "有点沮丧但能学到东西",
          "是一件丢脸的事", "无法接受"]),
        ("fq", "你对被动收入（理财收益等）的态度？",
         ["已在积极构建多条被动收入", "了解重要性并开始尝试", "有兴趣但还没行动",
          "觉得不靠谱", "只相信劳动收入"]),
        ("sq", "你的同理心水平如何？",
         ["能深度共情并保持理性", "很容易共情他人", "对亲近的人有同理心",
          "理性居多，不太感性", "很难感受他人情绪"]),
        ("hq", "你每年体检的习惯？",
         ["定期全面体检并有健康档案", "每年基本体检一次", "偶尔想到才去",
          "好几年没体检了", "从不体检"]),
        ("hq", "你的睡眠质量如何？",
         ["每天7-8小时，深度睡眠充足", "基本规律偶尔失眠", "经常熬夜睡眠不足",
          "严重失眠或作息颠倒", "长期靠药物入睡"]),
        ("iq", "你玩策略类游戏或解谜游戏的表现？",
         ["总能找到最优解", "玩得不错", "水平中等",
          "不太擅长", "完全玩不来"]),
        ("eq", "在聚会或社交场合中你通常扮演什么角色？",
         ["活跃气氛的组织者", "积极参与者", "安静但不尴尬的存在",
          "想早点离开", "极度不适避免参加"]),
        ("aq", "面对突发危机（如停电、系统崩溃），你？",
         ["冷静应对快速解决", "有些紧张但能处理", "慌乱但最终能解决",
          "需要别人来指挥", "完全不知所措"]),
        ("fq", "冲动消费的频率？",
         ["几乎从来不会", "偶尔会有但金额小", "平均每月一次",
          "每周都有", "每天都在买不需要的东西"]),
        ("sq", "你对艺术（音乐、绘画、文学）的感受力？",
         "非常热爱且有深入理解",
         "比较喜欢，有自己的品味",
         "有一定兴趣但不深入",
         "不太感兴趣",
         "完全无感"]),
        ("hq", "你每天喝水的习惯？",
         ["2升以上，有规律饮水", "1.5-2升", "1升左右",
          "渴了才喝", "基本只喝饮料不喝水"]),
    ]

    start_sort = 100
    for i, (cat, q, opts) in enumerate(paid_questions):
        db.add(Question(
            category=cat,
            question_text=q,
            options=opts if isinstance(opts, list) else [
                opts, "比较喜欢，有自己的品味", "有一定兴趣但不深入",
                "不太感兴趣", "完全无感"
            ],
            scores=[5, 4, 3, 2, 1],
            sort_order=start_sort + i,
            is_paid=True,
            is_active=True,
        ))
    db.commit()
    return JSONResponse({"msg": "种子数据已导入", "free": 14, "paid": len(paid_questions)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
