import os, sqlite3, secrets, hashlib, hmac, json, io, urllib.request, urllib.parse, time, threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image, ImageOps
from product_ai import analyze_product_ai

BASE=Path(__file__).parent
DB=Path(os.getenv("DATABASE_PATH", str(BASE/"app.db")))
DB.parent.mkdir(parents=True, exist_ok=True)
UPLOADS=Path(os.getenv("UPLOADS_PATH", str(BASE/"uploads")))
UPLOADS.mkdir(parents=True, exist_ok=True)

app=FastAPI(title="Бизнес из дома — помощник продаж")
app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")
app.mount("/uploads",StaticFiles(directory=UPLOADS),name="uploads")

def con():
    c=sqlite3.connect(DB, timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c

def init():
    c=con()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE,
      password_hash TEXT,
      name TEXT,
      goal REAL DEFAULT 70000,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY,
      user_id INTEGER,
      expires_at TEXT
    );
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      name TEXT,
      category TEXT,
      condition TEXT,
      photo_url TEXT,
      price REAL DEFAULT 0,
      status TEXT DEFAULT 'draft',
      listing_title TEXT DEFAULT '',
      listing_description TEXT DEFAULT '',
      sold_price REAL DEFAULT 0,
      buy_price REAL DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      sold_at TEXT
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      event_name TEXT,
      product_id INTEGER,
      meta TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS telegram_groups(
      media_group_id TEXT PRIMARY KEY,
      user_id INTEGER,
      product_id INTEGER,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS salary_withdrawals(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      amount REAL NOT NULL,
      note TEXT DEFAULT '',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS telegram_updates(
      update_id TEXT PRIMARY KEY,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS lots(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      name TEXT NOT NULL,
      purchase_cost REAL DEFAULT 0,
      note TEXT DEFAULT '',
      status TEXT DEFAULT 'active',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    ucols={r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    for col,ddl in [
      ("telegram_user_id","ALTER TABLE users ADD COLUMN telegram_user_id TEXT"),
      ("telegram_chat_id","ALTER TABLE users ADD COLUMN telegram_chat_id TEXT"),
      ("telegram_link_token","ALTER TABLE users ADD COLUMN telegram_link_token TEXT"),
      ("default_source","ALTER TABLE users ADD COLUMN default_source TEXT DEFAULT 'home'"),
      ("business_modes","ALTER TABLE users ADD COLUMN business_modes TEXT DEFAULT '[]'"),
      ("salary_percent","ALTER TABLE users ADD COLUMN salary_percent REAL DEFAULT 30"),
      ("goal_label","ALTER TABLE users ADD COLUMN goal_label TEXT DEFAULT ''"),
      ("pending_product_id","ALTER TABLE users ADD COLUMN pending_product_id INTEGER"),
      ("pending_mode","ALTER TABLE users ADD COLUMN pending_mode TEXT DEFAULT ''"),
      ("active_lot_id","ALTER TABLE users ADD COLUMN active_lot_id INTEGER"),
      ("lot_batch_mode","ALTER TABLE users ADD COLUMN lot_batch_mode INTEGER DEFAULT 0")
    ]:
        if col not in ucols: c.execute(ddl)
    pcols={r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    for col,ddl in [
      ("photos_json","ALTER TABLE products ADD COLUMN photos_json TEXT DEFAULT '[]'"),
      ("source","ALTER TABLE products ADD COLUMN source TEXT DEFAULT 'web'"),
      ("telegram_message_id","ALTER TABLE products ADD COLUMN telegram_message_id TEXT"),
      ("source_type","ALTER TABLE products ADD COLUMN source_type TEXT DEFAULT 'home'"),
      ("sale_expenses","ALTER TABLE products ADD COLUMN sale_expenses REAL DEFAULT 0"),
      ("listed_at","ALTER TABLE products ADD COLUMN listed_at TEXT"),
      ("ai_json","ALTER TABLE products ADD COLUMN ai_json TEXT DEFAULT '{}'"),
      ("ai_status","ALTER TABLE products ADD COLUMN ai_status TEXT DEFAULT 'waiting'"),
      ("ai_updated_at","ALTER TABLE products ADD COLUMN ai_updated_at TEXT"),
      ("user_notes","ALTER TABLE products ADD COLUMN user_notes TEXT DEFAULT ''"),
      ("lot_id","ALTER TABLE products ADD COLUMN lot_id INTEGER")
    ]:
        if col not in pcols: c.execute(ddl)
    lcols={r["name"] for r in c.execute("PRAGMA table_info(lots)").fetchall()}
    for col,ddl in [
      ("expected_items","ALTER TABLE lots ADD COLUMN expected_items INTEGER DEFAULT 0")
    ]:
        if col not in lcols: c.execute(ddl)
    gcols={r["name"] for r in c.execute("PRAGMA table_info(telegram_groups)").fetchall()}
    for col,ddl in [
      ("updated_at","ALTER TABLE telegram_groups ADD COLUMN updated_at REAL DEFAULT 0"),
      ("analyzed_at","ALTER TABLE telegram_groups ADD COLUMN analyzed_at REAL DEFAULT 0")
    ]:
        if col not in gcols: c.execute(ddl)
    c.commit()
    c.close()
init()

def hp(p):
    salt=secrets.token_hex(16)
    d=hashlib.pbkdf2_hmac("sha256",p.encode(),bytes.fromhex(salt),150000).hex()
    return salt+"$"+d

def vp(p,s):
    try:
        salt,d=s.split("$",1)
        got=hashlib.pbkdf2_hmac("sha256",p.encode(),bytes.fromhex(salt),150000).hex()
        return hmac.compare_digest(got,d)
    except Exception:
        return False

def user(auth):
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(401,"Нужно войти")
    token=auth.split(" ",1)[1]
    c=con()
    r=c.execute("""
      SELECT u.* FROM sessions s
      JOIN users u ON u.id=s.user_id
      WHERE s.token=? AND s.expires_at>?
    """,(token,datetime.now(timezone.utc).isoformat())).fetchone()
    c.close()
    if not r:
        raise HTTPException(401,"Сессия истекла")
    return dict(r)

def event(uid,name,pid=None,meta=None):
    c=con()
    c.execute("INSERT INTO events(user_id,event_name,product_id,meta) VALUES(?,?,?,?)",
              (uid,name,pid,json.dumps(meta or {},ensure_ascii=False)))
    c.commit()
    c.close()

def ai_client():
    key=os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key)
    except Exception:
        return None

def ask_ai(instructions,prompt,image_url=None,image_urls=None):
    cli=ai_client()
    if not cli:
        return None
    try:
        content=[{"type":"input_text","text":prompt}]
        urls=[]
        if image_urls: urls.extend(image_urls)
        elif image_url: urls.append(image_url)
        import base64, mimetypes
        for url in urls[:8]:
            if url and url.startswith("/uploads/"):
                path=UPLOADS/Path(url).name
                if path.exists():
                    mime=mimetypes.guess_type(path.name)[0] or "image/jpeg"
                    data=base64.b64encode(path.read_bytes()).decode()
                    content.append({"type":"input_image","image_url":"data:"+mime+";base64,"+data})
        r=cli.responses.create(
            model=os.getenv("OPENAI_MODEL","gpt-5.6-luna"),
            instructions=instructions,
            input=[{"role":"user","content":content}]
        )
        return r.output_text
    except Exception:
        return None

KOLYA_PROMPT="""Ты — Коля AI, помощник продавца проекта «Бизнес из дома» Леры и Вики.
Ты говоришь по-человечески: коротко, понятно, без заумных слов и канцелярита.
Твой стиль: «давай посмотрим», «есть за что зацепиться», «докрутим», «превратим в деньги», «не спешим отдавать дёшево», «покупатель не обязан угадывать».
Не используй слова «оптимизация», «монетизация», «целевая аудитория», «конверсия», если можно сказать проще.
Не придумывай бренд, модель, размер, материал, состояние или характеристики, которых не видно и которых пользователь не сообщил.
Учитывай модель продавца: свои вещи, вещи знакомых, лоты/сток/возвраты, закупка под перепродажу, остатки бизнеса.
Для лотов сначала помогай вернуть закупку и выделить самые денежные позиции.
Для закупки считай: закупка → цена продажи → сколько останется.
Для личных вещей не перегружай: 1 главная мысль + 2–3 действия.
Цена — всегда как ориентир, а не гарантия. Если нет данных рынка, честно говори, что это ориентир по истории пользователя и товару.
Каждый ответ заканчивай понятным следующим действием."""
 
def telegram_api(method,payload=None):
    token=os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    data=None
    headers={}
    if payload is not None:
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
        headers["Content-Type"]="application/json"
    req=urllib.request.Request("https://api.telegram.org/bot"+token+"/"+method,data=data,headers=headers)
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def telegram_send(chat_id,text,reply_markup=None):
    try:
        payload={"chat_id":chat_id,"text":text}
        if reply_markup is not None:
            payload["reply_markup"]=reply_markup
        telegram_api("sendMessage",payload)
    except Exception:
        pass

def source_keyboard():
    return {"inline_keyboard":[
      [{"text":"🏠 Дом","callback_data":"source:home"},{"text":"📦 Лоты","callback_data":"source:lot"}],
      [{"text":"🛒 Закупка","callback_data":"source:buy"},{"text":"🏪 Остатки","callback_data":"source:business"}],
      [{"text":"👥 Знакомые","callback_data":"source:friends"}]
    ]}

def product_continue_keyboard(has_missing=False):
    rows=[]
    if has_missing:
        rows.append([{"text":"📷 Дослать фото","callback_data":"product:attach"}])
    rows.append([{"text":"➕ Новый товар","callback_data":"product:new"}])
    return {"inline_keyboard":rows}

def lot_progress_keyboard():
    return {"inline_keyboard":[
      [{"text":"📊 Итог по лоту","callback_data":"lot:summary"}],
      [{"text":"✅ Закончить разбор лота","callback_data":"lot:finish"}]
    ]}

def lot_progress_for_product(uid,pid):
    c=con()
    p=c.execute("SELECT lot_id FROM products WHERE id=? AND user_id=?",(pid,uid)).fetchone()
    if not p or not p["lot_id"]:
        c.close(); return None
    lot=c.execute("SELECT * FROM lots WHERE id=? AND user_id=?",(p["lot_id"],uid)).fetchone()
    if not lot:
        c.close(); return None
    items=[dict(x) for x in c.execute(
      "SELECT status,price,listing_title,sold_price FROM products WHERE user_id=? AND lot_id=?",(uid,lot["id"])
    ).fetchall()]
    c.close()
    total=len(items)
    ready=sum(1 for x in items if x.get("listing_title"))
    sold=sum(1 for x in items if x["status"]=="sold")
    returned=sum(float(x.get("sold_price") or 0) for x in items if x["status"]=="sold")
    potential=sum(float(x.get("price") or 0) for x in items if x["status"]!="sold")
    expected=int(lot["expected_items"] or 0)
    purchase=float(lot["purchase_cost"] or 0)
    remaining=max(purchase-returned,0)
    target=(" из "+str(expected)) if expected else ""
    return {
      "lot_id":lot["id"],"name":lot["name"],"total":total,"expected":expected,"ready":ready,
      "sold":sold,"returned":returned,"potential":potential,"remaining":remaining,
      "text":"📦 "+str(lot["name"])+"\nРазобрано: "+str(total)+target+
             "\nГотово к публикации: "+str(ready)+
             "\nПродано: "+str(sold)+
             "\nВернулось: "+str(int(returned))+" ₽"+
             "\nОсталось отбить: "+str(int(remaining))+" ₽"+
             "\nПотенциал непроданных по текущим карточкам: "+str(int(potential))+" ₽"
    }

def resale_hint(price,buy_price):
    try:
        price=float(price or 0); buy_price=float(buy_price or 0)
    except Exception:
        price=buy_price=0
    if buy_price<=0:
        return ""
    if price<=0:
        return "Закуп записал: "+str(int(buy_price))+" ₽. Когда определим цену продажи — покажу потенциальную прибыль."
    profit=price-buy_price
    margin=(profit/buy_price*100) if buy_price else 0
    if profit>=0:
        return "Закуп: "+str(int(buy_price))+" ₽\nЦена продажи: "+str(int(price))+" ₽\nПотенциально останется: "+str(int(profit))+" ₽ ("+str(int(margin))+"% к закупке)"
    return "Закуп: "+str(int(buy_price))+" ₽\nТекущая цена ниже закупки на "+str(int(abs(profit)))+" ₽. Проверь цену перед публикацией."

def telegram_bot_username():
    configured=os.getenv("TELEGRAM_BOT_USERNAME","").lstrip("@").strip()
    if configured:
        return configured
    try:
        me=telegram_api("getMe")
        return (me or {}).get("result",{}).get("username","")
    except Exception:
        return ""

def telegram_configure_webhook():
    public_url=os.getenv("APP_PUBLIC_URL","").rstrip("/")
    secret=os.getenv("TELEGRAM_WEBHOOK_SECRET","")
    if not public_url or not os.getenv("TELEGRAM_BOT_TOKEN"):
        return False
    payload={"url":public_url+"/telegram/webhook","allowed_updates":["message","callback_query"]}
    if secret:
        payload["secret_token"]=secret
    try:
        result=telegram_api("setWebhook",payload)
        return bool(result and result.get("ok"))
    except Exception:
        return False

@app.on_event("startup")
def setup_telegram_webhook():
    telegram_configure_webhook()

def telegram_download_photo(file_id):
    meta=telegram_api("getFile",{"file_id":file_id})
    path=meta["result"]["file_path"]
    token=os.getenv("TELEGRAM_BOT_TOKEN")
    raw=urllib.request.urlopen("https://api.telegram.org/file/bot"+token+"/"+path,timeout=30).read()
    fn=secrets.token_hex(8)+".jpg"
    out=UPLOADS/fn
    try:
        img=Image.open(io.BytesIO(raw))
        img=ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1800,1800))
        img.save(out,"JPEG",quality=90,optimize=True)
    except Exception:
        out.write_bytes(raw)
    return "/uploads/"+fn

def ai_done_message(data):
    if not data:
        return "Карточку сохранил 💜"
    try:
        lo=float(data.get("suggested_price_low") or 0)
        hi=float(data.get("suggested_price_high") or 0)
    except Exception:
        lo=hi=0
    price=("\nОриентир: "+str(int(lo or hi))+"–"+str(int(hi or lo))+" ₽") if (lo or hi) else ""
    value=str(data.get("buyer_value") or "").strip()
    missing=data.get("needs_clarification") or []
    tail="\n\nЯ уже собрал карточку и готовое объявление 💜"
    if missing:
        tail+="\n\nМне не хватает пары фактов:\n• "+"\n• ".join([str(x) for x in missing[:3]])+"\n\nМожешь ответить текстом или просто дослать нужное фото — я добавлю его к этому товару и сам обновлю карточку. Если это уже новый товар, нажми «➕ Новый товар»."
    else:
        tail+=" Открой приложение — останется проверить и скопировать."
    return "Готово 👀\n"+str(data.get("name") or "Товар")+price+("\n\n"+value if value else "")+tail

def analyze_single_background(pid,uid,chat_id):
    data=analyze_product_ai(pid,uid,con,ask_ai,event,KOLYA_PROMPT)
    missing=bool((data or {}).get("needs_clarification") or (data or {}).get("missing_photos"))
    c=con()
    p=c.execute("SELECT source_type,buy_price,price FROM products WHERE id=? AND user_id=?",(pid,uid)).fetchone()
    u=c.execute("SELECT lot_batch_mode FROM users WHERE id=?",(uid,)).fetchone()
    mode="clarify" if missing else ("buy_cost" if p and p["source_type"]=="buy" and not (p["buy_price"] or 0) else "")
    c.execute("UPDATE users SET pending_product_id=?,pending_mode=? WHERE id=?",(pid,mode,uid))
    c.commit(); c.close()
    progress=lot_progress_for_product(uid,pid) if u and u["lot_batch_mode"] else None
    if progress:
        telegram_send(chat_id,"Готово 👀 "+str((data or {}).get("name") or "Товар")+"\n\n"+progress["text"],lot_progress_keyboard())
    elif mode=="buy_cost":
        telegram_send(chat_id,ai_done_message(data)+"\n\n💸 За сколько купила этот товар? Напиши только сумму, например: 480")
    else:
        telegram_send(chat_id,ai_done_message(data),product_continue_keyboard(missing))

def analyze_album_background(media_group_id,pid,uid,chat_id):
    time.sleep(2.6)
    c=con()
    g=c.execute("SELECT * FROM telegram_groups WHERE media_group_id=?",(str(media_group_id),)).fetchone()
    if not g:
        c.close()
        return
    updated=float(g["updated_at"] or 0)
    analyzed=float(g["analyzed_at"] or 0)
    if analyzed>=updated or time.time()-updated<2.0:
        c.close()
        return
    cur=c.execute("UPDATE telegram_groups SET analyzed_at=? WHERE media_group_id=? AND analyzed_at<?",(updated,str(media_group_id),updated))
    c.commit()
    won=cur.rowcount>0
    c.close()
    if not won:
        return
    data=analyze_product_ai(pid,uid,con,ask_ai,event,KOLYA_PROMPT)
    missing=bool((data or {}).get("needs_clarification") or (data or {}).get("missing_photos"))
    c=con()
    p=c.execute("SELECT source_type,buy_price,price FROM products WHERE id=? AND user_id=?",(pid,uid)).fetchone()
    u=c.execute("SELECT lot_batch_mode FROM users WHERE id=?",(uid,)).fetchone()
    mode="clarify" if missing else ("buy_cost" if p and p["source_type"]=="buy" and not (p["buy_price"] or 0) else "")
    c.execute("UPDATE users SET pending_product_id=?,pending_mode=? WHERE id=?",(pid,mode,uid))
    c.commit(); c.close()
    progress=lot_progress_for_product(uid,pid) if u and u["lot_batch_mode"] else None
    if progress:
        telegram_send(chat_id,"Готово 👀 "+str((data or {}).get("name") or "Товар")+"\n\n"+progress["text"],lot_progress_keyboard())
    elif mode=="buy_cost":
        telegram_send(chat_id,ai_done_message(data)+"\n\n💸 За сколько купила этот товар? Напиши только сумму, например: 480")
    else:
        telegram_send(chat_id,ai_done_message(data),product_continue_keyboard(missing))

class Register(BaseModel):
    name:str
    email:str
    password:str
    goal:float=70000

class Login(BaseModel):
    email:str
    password:str

class Sale(BaseModel):
    sold_price:float
    buy_price:float=0
    expenses:float=0

class PurchaseCost(BaseModel):
    buy_price:float

class Chat(BaseModel):
    text:str

class ProfileUpdate(BaseModel):
    goal:Optional[float]=None
    default_source:Optional[str]=None
    business_modes:Optional[list[str]]=None
    salary_percent:Optional[float]=None
    goal_label:Optional[str]=None

class SalaryTake(BaseModel):
    amount:float
    note:str=""

class Clarification(BaseModel):
    text:str

class LotCreate(BaseModel):
    name:str
    purchase_cost:float=0
    note:str=""
    expected_items:int=0

@app.get("/")
def home():
    return FileResponse(BASE/"static"/"index.html",headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache"})

@app.get("/health")
def health():
    return {"ok":True,"ai":bool(os.getenv("OPENAI_API_KEY"))}

@app.post("/api/register")
def register(d:Register):
    if len(d.password)<6:
        raise HTTPException(400,"Пароль минимум 6 символов")
    c=con()
    try:
        cur=c.execute(
            "INSERT INTO users(email,password_hash,name,goal) VALUES(?,?,?,?)",
            (d.email.lower().strip(),hp(d.password),d.name.strip() or "Пользователь",d.goal)
        )
        uid=cur.lastrowid
        token=secrets.token_urlsafe(32)
        exp=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()
        c.execute("INSERT INTO sessions VALUES(?,?,?)",(token,uid,exp))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        raise HTTPException(409,"Такой email уже зарегистрирован")
    c.close()
    event(uid,"REGISTERED")
    return {"token":token}

@app.post("/api/login")
def login(d:Login):
    c=con()
    u=c.execute("SELECT * FROM users WHERE email=?",(d.email.lower().strip(),)).fetchone()
    if not u or not vp(d.password,u["password_hash"]):
        c.close()
        raise HTTPException(401,"Неверный email или пароль")
    token=secrets.token_urlsafe(32)
    exp=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()
    c.execute("INSERT INTO sessions VALUES(?,?,?)",(token,u["id"],exp))
    c.commit()
    c.close()
    return {"token":token}

@app.get("/api/state")
def state(authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    ps=[dict(x) for x in c.execute(
        "SELECT * FROM products WHERE user_id=? ORDER BY id DESC",(u["id"],)
    ).fetchall()]
    taken=c.execute("SELECT COALESCE(SUM(amount),0) AS total FROM salary_withdrawals WHERE user_id=?",(u["id"],)).fetchone()["total"] or 0
    withdrawals=[dict(x) for x in c.execute("SELECT * FROM salary_withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20",(u["id"],)).fetchall()]
    lots_raw=[dict(x) for x in c.execute("SELECT * FROM lots WHERE user_id=? ORDER BY id DESC",(u["id"],)).fetchall()]
    c.close()
    sold=[p for p in ps if p["status"]=="sold"]
    revenue=sum(p["sold_price"] or 0 for p in sold)
    profit=sum((p["sold_price"] or 0)-(p["buy_price"] or 0)-(p.get("sale_expenses") or 0) for p in sold)
    counts={
      "new":sum(1 for p in ps if p["status"]=="draft"),
      "listed":sum(1 for p in ps if p["status"]=="listed"),
      "sold":len(sold)
    }
    stale=[]
    now_naive=datetime.utcnow()
    for p in ps:
        if p["status"]!="listed" or not p.get("listed_at"):
            continue
        try:
            dt=datetime.fromisoformat(str(p["listed_at"]).replace("Z",""))
            days=max(0,(now_naive-dt).days)
        except Exception:
            days=0
        if days>=7:
            stale.append({"id":p["id"],"name":p["name"],"days":days,"price":p["price"]})
    by_source={}
    for p in sold:
        key=p.get("source_type") or "home"
        row=by_source.setdefault(key,{"revenue":0,"profit":0,"sold":0})
        row["revenue"]+=(p["sold_price"] or 0)
        row["profit"]+=(p["sold_price"] or 0)-(p["buy_price"] or 0)-(p.get("sale_expenses") or 0)
        row["sold"]+=1

    category_stats={}
    for p in ps:
        cat=(p.get("category") or "Без категории").strip() or "Без категории"
        row=category_stats.setdefault(cat,{"listed":0,"sold":0,"revenue":0,"profit":0,"days_total":0,"days_count":0})
        if p["status"]=="listed":
            row["listed"]+=1
        if p["status"]=="sold":
            row["sold"]+=1
            row["revenue"]+=(p["sold_price"] or 0)
            row["profit"]+=(p["sold_price"] or 0)-(p["buy_price"] or 0)-(p.get("sale_expenses") or 0)
            try:
                if p.get("listed_at") and p.get("sold_at"):
                    a=datetime.fromisoformat(str(p["listed_at"]).replace("Z",""))
                    b=datetime.fromisoformat(str(p["sold_at"]).replace("Z",""))
                    row["days_total"]+=max(0,(b-a).days)
                    row["days_count"]+=1
            except Exception:
                pass
    category_insights=[]
    for cat,row in category_stats.items():
        avg_days=(row["days_total"]/row["days_count"]) if row["days_count"] else None
        avg_profit=(row["profit"]/row["sold"]) if row["sold"] else 0
        category_insights.append({
          "category":cat,"listed":row["listed"],"sold":row["sold"],
          "revenue":row["revenue"],"profit":row["profit"],
          "avg_profit":avg_profit,"avg_days":avg_days
        })
    category_insights.sort(key=lambda x:(x["sold"],x["profit"]),reverse=True)
    # Короткий план дня: максимум 3 действия, только из реальных данных пользователя.
    ready_drafts=[p for p in ps if p["status"]=="draft" and p.get("listing_title")]
    incomplete_drafts=[p for p in ps if p["status"]=="draft" and not p.get("listing_title")]
    sold_profits=[(p["sold_price"] or 0)-(p["buy_price"] or 0)-(p.get("sale_expenses") or 0) for p in sold]
    positive_profits=[x for x in sold_profits if x>0]
    avg_profit_all=(sum(positive_profits)/len(positive_profits)) if positive_profits else 0
    goal_amount=float(u.get("goal") or 0)
    goal_left=max(goal_amount-revenue,0)
    sales_to_goal=int((goal_left+avg_profit_all-1)//avg_profit_all) if goal_left>0 and avg_profit_all>0 else None

    best_categories=[x for x in category_insights if x["sold"]>0]
    best_category=best_categories[0]["category"] if best_categories else None
    daily_actions=[]
    if ready_drafts:
        pick=next((p for p in ready_drafts if best_category and (p.get("category") or "")==best_category),ready_drafts[0])
        daily_actions.append({"type":"publish","product_id":pick["id"],"text":"Выставить «"+str(pick.get("name") or "готовый товар")+"» — карточка уже собрана."})
    elif incomplete_drafts:
        pick=incomplete_drafts[0]
        daily_actions.append({"type":"finish","product_id":pick["id"],"text":"Докрутить «"+str(pick.get("name") or "товар")+"» — ему ещё не хватает готовой карточки."})
    else:
        daily_actions.append({"type":"add","product_id":None,"text":"Отправить Коле 1–3 новых товара в Telegram."})

    if stale:
        pick=stale[0]
        daily_actions.append({"type":"boost","product_id":pick["id"],"text":"Проверить «"+str(pick.get("name") or "товар")+"» — уже "+str(pick["days"])+" дн. в продаже."})

    active_listed=[p for p in ps if p["status"]=="listed"]
    if len(daily_actions)<3 and active_listed:
        daily_actions.append({"type":"sales","product_id":active_listed[0]["id"],"text":"Не забыть отметить продажу, если кто-то из активных товаров уже ушёл."})
    if len(daily_actions)<3 and not ready_drafts and not incomplete_drafts:
        daily_actions.append({"type":"add","product_id":None,"text":"Добавить ещё один товар — так Коля быстрее накопит твою личную статистику."})
    daily_actions=daily_actions[:3]

    if sales_to_goal is not None:
        goal_hint="До цели осталось "+str(int(goal_left))+" ₽ — по твоей средней прибыли это примерно "+str(sales_to_goal)+" продаж(и)."
    elif goal_left>0:
        goal_hint="До цели осталось "+str(int(goal_left))+" ₽. После нескольких продаж Коля сможет считать путь точнее."
    else:
        goal_hint="Цель уже закрыта 💜 Можно зафиксировать результат и поставить следующую."

    lot_summaries=[]
    for lot in lots_raw:
        lp=[p for p in ps if p.get("lot_id")==lot["id"]]
        ls=[p for p in lp if p["status"]=="sold"]
        returned=sum((p.get("sold_price") or 0) for p in ls)
        purchase=float(lot.get("purchase_cost") or 0)
        remaining=max(purchase-returned,0)
        pure_profit=max(returned-purchase,0)
        lot_summaries.append({
          "id":lot["id"],"name":lot["name"],"purchase_cost":purchase,"note":lot.get("note") or "",
          "status":lot.get("status") or "active","items":len(lp),
          "expected_items":int(lot.get("expected_items") or 0),
          "new":sum(1 for p in lp if p["status"]=="draft"),
          "ready":sum(1 for p in lp if bool(p.get("listing_title"))),
          "listed":sum(1 for p in lp if p["status"]=="listed"),
          "sold":len(ls),"returned":returned,"remaining":remaining,
          "potential_unsold":sum(float(p.get("price") or 0) for p in lp if p["status"]!="sold"),
          "potential_total":returned+sum(float(p.get("price") or 0) for p in lp if p["status"]!="sold"),
          "pure_profit":pure_profit,"recouped":returned>=purchase if purchase>0 else bool(returned)
        })

    try: modes=json.loads(u.get("business_modes") or "[]")
    except Exception: modes=[]
    salary_percent=float(u.get("salary_percent") or 30)
    salary_target=max(profit,0)*salary_percent/100
    return {
        "profile":{
          "name":u["name"],"goal":u["goal"],"goal_label":u.get("goal_label") or "",
          "default_source":u.get("default_source") or "home","business_modes":modes,
          "salary_percent":salary_percent,"active_lot_id":u.get("active_lot_id"),
          "lot_batch_mode":bool(u.get("lot_batch_mode"))
        },
        "products":ps,
        "lots":lot_summaries,
        "withdrawals":withdrawals,
        "stats":{
          "revenue":revenue,"earned":revenue,"profit":profit,"sold":len(sold),
          "avg":revenue/len(sold) if sold else 0,"counts":counts,"by_source":by_source,
          "salary_taken":taken,"salary_target":salary_target,"salary_available":max(salary_target-taken,0),
          "stale_count":len(stale),"stale_products":stale[:5],
          "category_insights":category_insights[:8],
          "daily_plan":{"actions":daily_actions,"goal_hint":goal_hint,"best_category":best_category,"sales_to_goal":sales_to_goal}
        }
    }


@app.post("/api/profile")
def update_profile(d:ProfileUpdate,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    fields=[]; vals=[]
    if d.goal is not None: fields.append("goal=?"); vals.append(d.goal)
    if d.default_source is not None: fields.append("default_source=?"); vals.append(d.default_source)
    if d.business_modes is not None: fields.append("business_modes=?"); vals.append(json.dumps(d.business_modes,ensure_ascii=False))
    if d.salary_percent is not None:
        fields.append("salary_percent=?"); vals.append(max(0,min(100,d.salary_percent)))
    if d.goal_label is not None: fields.append("goal_label=?"); vals.append(d.goal_label[:80])
    if fields:
        vals.append(u["id"])
        c=con(); c.execute("UPDATE users SET "+",".join(fields)+" WHERE id=?",tuple(vals)); c.commit(); c.close()
    return {"ok":True}

@app.post("/api/lots")
def create_lot(d:LotCreate,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    name=d.name.strip()
    if not name:
        raise HTTPException(400,"Дай лоту название")
    c=con()
    cur=c.execute("INSERT INTO lots(user_id,name,purchase_cost,note,expected_items) VALUES(?,?,?,?,?)",
                  (u["id"],name,max(0,d.purchase_cost),d.note[:300],max(0,d.expected_items)))
    lid=cur.lastrowid
    c.execute("UPDATE users SET active_lot_id=?,default_source='lot',lot_batch_mode=1 WHERE id=?",(lid,u["id"]))
    c.commit(); c.close()
    event(u["id"],"LOT_CREATED",None,{"lot_id":lid,"purchase_cost":d.purchase_cost})
    return {"id":lid}

@app.post("/api/lots/{lid}/strategy")
def lot_strategy(lid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    lot=c.execute("SELECT * FROM lots WHERE id=? AND user_id=?",(lid,u["id"])).fetchone()
    if not lot:
        c.close(); raise HTTPException(404,"Лот не найден")
    items=[dict(x) for x in c.execute(
        "SELECT id,name,category,price,status,listing_title,ai_json,sold_price FROM products WHERE user_id=? AND lot_id=? ORDER BY id DESC",
        (u["id"],lid)
    ).fetchall()]
    c.close()
    sold=[p for p in items if p["status"]=="sold"]
    returned=sum((p.get("sold_price") or 0) for p in sold)
    remaining=max(float(lot["purchase_cost"] or 0)-returned,0)

    compact=[]
    for p in items:
        ai={}
        try: ai=json.loads(p.get("ai_json") or "{}")
        except Exception: ai={}
        compact.append({
          "id":p["id"],"name":p["name"],"category":p["category"],"price":p["price"],
          "status":p["status"],"listing_ready":bool(p.get("listing_title")),
          "buyer_value":ai.get("buyer_value"),"condition":ai.get("condition"),
          "suggested_price_low":ai.get("suggested_price_low"),"suggested_price_high":ai.get("suggested_price_high")
        })

    result=ask_ai(
        KOLYA_PROMPT+"""
Ты помогаешь разбирать лот/паллету. Главная задача — сначала вернуть закуп, потом считать прибыль.
Используй только реальные товары и цены из списка. Не придумывай спрос площадки, просмотры или рыночные цены.
Ответь коротко и практично:
1) ТОП-3: какие конкретно товары выставить первыми и почему.
2) БЫСТРЫЕ ДЕНЬГИ: что из уже готового может быстрее вернуть часть закупа.
3) КОМПЛЕКТЫ: какие недорогие похожие товары можно объединить, только если это логично по категориям/назначению.
4) СЕЙЧАС: одно действие на ближайшие 20 минут.
Для каждого товара указывай его id в виде #123, чтобы приложение могло показать его пользователю.
Не советуй занижать цену без данных.""",
        "Лот: "+str(lot["name"])+"\nЗакуп: "+str(lot["purchase_cost"] or 0)+
        "\nУже вернулось: "+str(returned)+"\nОсталось отбить: "+str(remaining)+
        "\nТовары: "+json.dumps(compact,ensure_ascii=False)
    )
    if not result:
        unsold=[p for p in items if p["status"]!="sold"]
        ready=[p for p in unsold if p.get("listing_title")]
        ready.sort(key=lambda x:float(x.get("price") or 0),reverse=True)
        picks=ready[:3] if ready else unsold[:3]
        lines=[]
        for p in picks:
            lines.append("#"+str(p["id"])+" "+str(p.get("name") or "Товар")+" — "+str(int(p.get("price") or 0))+" ₽")
        result="ТОП-3:\n"+("\n".join(lines) if lines else "Пока в лоте нет товаров для приоритета.")+
               "\n\nБЫСТРЫЕ ДЕНЬГИ: начинай с готовых карточек с более высокой ценой — сначала возвращаем закуп."+
               "\n\nКОМПЛЕКТЫ: объединяй только мелкие однотипные товары, если по отдельности их неудобно продавать."+
               "\n\nСЕЙЧАС: выстави первый готовый товар из списка."
    event(u["id"],"LOT_STRATEGY_VIEWED",None,{"lot_id":lid})
    return {"text":result,"remaining":remaining}

@app.post("/api/lots/{lid}/activate")
def activate_lot(lid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    lot=c.execute("SELECT id FROM lots WHERE id=? AND user_id=?",(lid,u["id"])).fetchone()
    if not lot:
        c.close(); raise HTTPException(404,"Лот не найден")
    c.execute("UPDATE users SET active_lot_id=?,default_source='lot',lot_batch_mode=1 WHERE id=?",(lid,u["id"]))
    c.commit(); c.close()
    return {"ok":True}

@app.post("/api/lots/deactivate")
def deactivate_lot(authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con(); c.execute("UPDATE users SET active_lot_id=NULL,lot_batch_mode=0,default_source='home' WHERE id=?",(u["id"],)); c.commit(); c.close()
    return {"ok":True}

@app.post("/api/salary")
def take_salary(d:SalaryTake,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    if d.amount<=0: raise HTTPException(400,"Сумма должна быть больше нуля")
    c=con(); c.execute("INSERT INTO salary_withdrawals(user_id,amount,note) VALUES(?,?,?)",(u["id"],d.amount,d.note[:120])); c.commit(); c.close()
    event(u["id"],"SALARY_TAKEN",None,{"amount":d.amount})
    return {"ok":True}

@app.get("/api/telegram/status")
def telegram_status(authorization:Optional[str]=Header(None)):
    u=user(authorization)
    username=telegram_bot_username()
    return {"connected":bool(u.get("telegram_user_id")),"bot_username":username,"configured":bool(username)}

@app.post("/api/telegram/link")
def telegram_link(authorization:Optional[str]=Header(None)):
    u=user(authorization)
    username=telegram_bot_username()
    if not username:
        raise HTTPException(503,"Telegram-бот пока не настроен")
    token=secrets.token_urlsafe(18)
    c=con()
    c.execute("UPDATE users SET telegram_link_token=? WHERE id=?",(token,u["id"]))
    c.commit(); c.close()
    return {"url":"https://t.me/"+username+"?start="+token}

@app.post("/telegram/webhook")
async def telegram_webhook(request: __import__("fastapi").Request, x_telegram_bot_api_secret_token:Optional[str]=Header(None)):
    secret=os.getenv("TELEGRAM_WEBHOOK_SECRET","")
    if secret and x_telegram_bot_api_secret_token!=secret:
        raise HTTPException(403,"Bad webhook secret")
    update=await request.json()
    update_id=str(update.get("update_id",""))
    if update_id:
        c=con()
        try:
            c.execute("INSERT INTO telegram_updates(update_id) VALUES(?)",(update_id,))
            c.commit()
        except sqlite3.IntegrityError:
            c.close()
            return {"ok":True}
        c.close()

    cb=update.get("callback_query") or {}
    if cb:
        data=(cb.get("data") or "").strip()
        msg=cb.get("message") or {}
        chat_id=str((msg.get("chat") or {}).get("id",""))
        tg_user_id=str((cb.get("from") or {}).get("id",""))
        if data=="lot:summary":
            c=con()
            u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
            if u and u["active_lot_id"]:
                p=c.execute("SELECT id FROM products WHERE user_id=? AND lot_id=? ORDER BY id DESC LIMIT 1",(u["id"],u["active_lot_id"])).fetchone()
                lot=c.execute("SELECT * FROM lots WHERE id=? AND user_id=?",(u["active_lot_id"],u["id"])).fetchone()
                c.close()
                if p:
                    progress=lot_progress_for_product(u["id"],p["id"])
                    telegram_send(chat_id,progress["text"] if progress else "Пока не вижу товары в активном лоте.",lot_progress_keyboard())
                elif lot:
                    telegram_send(chat_id,"📦 "+str(lot["name"])+"\nПока 0 товаров. Кидай фото подряд — я сам сложу их в этот лот.",lot_progress_keyboard())
            else:
                c.close()
                telegram_send(chat_id,"Сейчас активного лота нет. Создай его в разделе «Деньги» 💜")
            return {"ok":True}
        if data=="lot:finish":
            c=con()
            u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
            if u:
                c.execute("UPDATE users SET active_lot_id=NULL,lot_batch_mode=0,default_source='home',pending_product_id=NULL,pending_mode='' WHERE id=?",(u["id"],))
                c.commit()
            c.close()
            telegram_send(chat_id,"Готово 💜 Разбор лота закончили. Следующие фото снова будут обычными товарами.")
            return {"ok":True}
        if data=="product:attach":
            c=con(); u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
            if u and u["pending_product_id"] and (u["pending_mode"] or "") in ("clarify","attach"):
                c.execute("UPDATE users SET pending_mode='attach' WHERE id=?",(u["id"],)); c.commit()
                p=c.execute("SELECT name FROM products WHERE id=? AND user_id=?",(u["pending_product_id"],u["id"])).fetchone()
                c.close()
                telegram_send(chat_id,"Да 💜 Досылай фото к товару «"+str((p["name"] if p else "товар"))+"». Можно одним фото или альбомом. Я добавлю их в эту же карточку и пересмотрю всё заново.")
            else:
                c.close()
                telegram_send(chat_id,"Сначала пришли основной товар, потом сможем дослать детали 💜")
            return {"ok":True}
        if data=="product:new":
            c=con(); u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
            if u:
                c.execute("UPDATE users SET pending_product_id=NULL,pending_mode='' WHERE id=?",(u["id"],)); c.commit()
            c.close()
            telegram_send(chat_id,"Готово. Следующее фото будет новым товаром 💜")
            return {"ok":True}
        if data.startswith("source:"):
            source=data.split(":",1)[1]
            labels={"home":"🏠 Дом","lot":"📦 Лоты","buy":"🛒 Закупка","business":"🏪 Остатки","friends":"👥 Знакомые"}
            if source in labels:
                c=con(); u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
                if u:
                    c.execute("UPDATE users SET default_source=? WHERE id=?",(source,u["id"])); c.commit()
                c.close()
                telegram_send(chat_id,"Готово 💜 Сейчас источник — "+labels[source]+". Всё, что пришлёшь дальше, сохраню туда. Можно просто кидать фото.")
            return {"ok":True}
        return {"ok":True}

    msg=update.get("message") or {}
    if not msg:
        return {"ok":True}
    chat_id=str((msg.get("chat") or {}).get("id",""))
    tg_user_id=str((msg.get("from") or {}).get("id",""))
    text=(msg.get("text") or "").strip()
    if text.startswith("/start"):
        parts=text.split(maxsplit=1)
        link_token=parts[1] if len(parts)>1 else ""
        c=con()
        u=c.execute("SELECT * FROM users WHERE telegram_link_token=?",(link_token,)).fetchone()
        if not u:
            c.close()
            telegram_send(chat_id,"Ссылка устарела. Открой помощник продаж и нажми «Подключить Telegram» ещё раз.")
            return {"ok":True}
        c.execute("UPDATE users SET telegram_user_id=?,telegram_chat_id=?,telegram_link_token=NULL WHERE id=?",(tg_user_id,chat_id,u["id"]))
        c.commit(); c.close()
        event(u["id"],"TELEGRAM_CONNECTED")
        telegram_send(chat_id,"Готово 💜 Я Коля AI. Сначала скажи, откуда сейчас будем разбирать товары:",source_keyboard())
        return {"ok":True}
    if text in ("/lot","Лот","лот"):
        c=con()
        u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
        if u and u["active_lot_id"]:
            p=c.execute("SELECT id FROM products WHERE user_id=? AND lot_id=? ORDER BY id DESC LIMIT 1",(u["id"],u["active_lot_id"])).fetchone()
            lot=c.execute("SELECT * FROM lots WHERE id=? AND user_id=?",(u["active_lot_id"],u["id"])).fetchone()
            c.close()
            if p:
                progress=lot_progress_for_product(u["id"],p["id"])
                telegram_send(chat_id,progress["text"] if progress else "Лот активен 💜",lot_progress_keyboard())
            elif lot:
                telegram_send(chat_id,"📦 "+str(lot["name"])+"\nПока 0 товаров. Кидай фото подряд — я сам считаю прогресс.",lot_progress_keyboard())
        else:
            c.close()
            telegram_send(chat_id,"Активного лота пока нет. Создай его в приложении в разделе «Деньги» 💜")
        return {"ok":True}
    if text in ("/new","Новый товар","новый товар"):
        c=con(); u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
        if u:
            c.execute("UPDATE users SET pending_product_id=NULL,pending_mode='' WHERE id=?",(u["id"],)); c.commit()
        c.close()
        telegram_send(chat_id,"Следующее фото считаю новым товаром 💜")
        return {"ok":True}
    if text in ("/source","Источник","источник"):
        c=con(); u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone(); c.close()
        if u:
            telegram_send(chat_id,"Откуда сейчас берём товары?",source_keyboard())
        else:
            telegram_send(chat_id,"Сначала подключи Telegram через приложение 💜")
        return {"ok":True}

    if text and not text.startswith("/"):
        c=con()
        u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
        if u and u["pending_product_id"] and (u["pending_mode"] or "")=="buy_cost":
            import re
            m=re.search(r'(\d[\d\s.,]*)',text)
            cost=0
            if m:
                raw=m.group(1).replace(" ","").replace(",",".")
                try: cost=float(raw)
                except Exception: cost=0
            if cost>0:
                pid=int(u["pending_product_id"])
                p=c.execute("SELECT name,price FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
                if p:
                    c.execute("UPDATE products SET buy_price=? WHERE id=? AND user_id=?",(cost,pid,u["id"]))
                    c.execute("UPDATE users SET pending_mode='' WHERE id=?",(u["id"],))
                    c.commit(); c.close()
                    event(u["id"],"PURCHASE_COST_SET",pid,{"buy_price":cost})
                    telegram_send(chat_id,"Записал 💜\n"+resale_hint(p["price"],cost)+"\n\nТеперь после продажи посчитаю реальную прибыль.",product_continue_keyboard(False))
                    return {"ok":True}
            c.close()
            telegram_send(chat_id,"Напиши сумму закупки цифрами, например: 480")
            return {"ok":True}
        if u and u["pending_product_id"] and (u["pending_mode"] or "") in ("clarify","attach"):
            pid=int(u["pending_product_id"])
            p=c.execute("SELECT user_notes,name FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
            if p:
                old=(p["user_notes"] or "").strip()
                merged=(old+"\n"+text).strip() if old else text
                c.execute("UPDATE products SET user_notes=?,ai_status='waiting' WHERE id=?",(merged,pid))
                c.commit(); c.close()
                telegram_send(chat_id,"Принял 💜 Добавляю это к товару «"+str(p["name"] or "товар")+"» и пересобираю карточку.")
                def _clarify():
                    data=analyze_product_ai(pid,u["id"],con,ask_ai,event,KOLYA_PROMPT)
                    event(u["id"],"PRODUCT_CLARIFIED",pid,{"text":text})
                    telegram_send(chat_id,ai_done_message(data))
                threading.Thread(target=_clarify,daemon=True).start()
                return {"ok":True}
        c.close()
        if u:
            telegram_send(chat_id,"Кидай фото нового товара 💜 Если это уточнение к текущему — нажми «📷 Дослать фото».")
            return {"ok":True}

    photos=msg.get("photo") or []
    if photos:
        c=con()
        u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
        if not u:
            c.close()
            telegram_send(chat_id,"Сначала свяжи Telegram с аккаунтом через кнопку в приложении 💜")
            return {"ok":True}
        url=telegram_download_photo(photos[-1]["file_id"])
        caption=(msg.get("caption") or "").strip()
        name=caption.splitlines()[0][:120] if caption else "Товар из Telegram"
        media_group_id=msg.get("media_group_id")

        if u["pending_product_id"] and (u["pending_mode"] or "") in ("clarify","attach"):
            pid=int(u["pending_product_id"])
            p=c.execute("SELECT photos_json,photo_url,name FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
            if p:
                arr=[]
                try: arr=json.loads(p["photos_json"] or "[]")
                except Exception: arr=[]
                if not arr and p["photo_url"]: arr=[p["photo_url"]]
                if url not in arr: arr.append(url)
                c.execute("UPDATE products SET photos_json=?,ai_status='waiting' WHERE id=? AND user_id=?",(json.dumps(arr),pid,u["id"]))
                first_extra=True
                if media_group_id:
                    grp=c.execute("SELECT * FROM telegram_groups WHERE media_group_id=?",(str(media_group_id),)).fetchone()
                    if grp:
                        first_extra=False
                        c.execute("UPDATE telegram_groups SET updated_at=? WHERE media_group_id=?",(time.time(),str(media_group_id)))
                    else:
                        c.execute("INSERT INTO telegram_groups(media_group_id,user_id,product_id,updated_at,analyzed_at) VALUES(?,?,?,?,0)",(str(media_group_id),u["id"],pid,time.time()))
                c.commit(); c.close()
                if first_extra:
                    telegram_send(chat_id,"Фото добавил к товару «"+str(p["name"] or "товар")+"» 💜 Пересматриваю карточку и объявление.")
                event(u["id"],"PRODUCT_EXTRA_PHOTO",pid)
                if media_group_id:
                    threading.Thread(target=analyze_album_background,args=(str(media_group_id),pid,u["id"],chat_id),daemon=True).start()
                else:
                    threading.Thread(target=analyze_single_background,args=(pid,u["id"],chat_id),daemon=True).start()
                return {"ok":True}

        pid=None
        created_new=False
        if media_group_id:
            grp=c.execute("SELECT * FROM telegram_groups WHERE media_group_id=?",(str(media_group_id),)).fetchone()
            if grp:
                pid=grp["product_id"]
                row=c.execute("SELECT photos_json,photo_url FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
                arr=[]
                try: arr=json.loads(row["photos_json"] or "[]")
                except Exception: pass
                if not arr and row["photo_url"]: arr=[row["photo_url"]]
                if url not in arr: arr.append(url)
                c.execute("UPDATE products SET photos_json=?,ai_status='waiting' WHERE id=?",(json.dumps(arr),pid))
                c.execute("UPDATE telegram_groups SET updated_at=? WHERE media_group_id=?",(time.time(),str(media_group_id)))
            else:
                arr=[url]
                cur=c.execute("""INSERT INTO products(user_id,name,category,condition,photo_url,photos_json,price,status,source,telegram_message_id)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",(u["id"],name,"","",url,json.dumps(arr),0,"draft","telegram",str(msg.get("message_id",""))))
                pid=cur.lastrowid
                c.execute("UPDATE products SET source_type=? WHERE id=?",((u["default_source"] if "default_source" in u.keys() and u["default_source"] else "home"),pid))
                if (u["default_source"] if "default_source" in u.keys() else "")=="lot" and ("active_lot_id" in u.keys()) and u["active_lot_id"]:
                    c.execute("UPDATE products SET lot_id=? WHERE id=?",(u["active_lot_id"],pid))
                c.execute("UPDATE users SET pending_product_id=? WHERE id=?",(pid,u["id"]))
                c.execute("INSERT INTO telegram_groups(media_group_id,user_id,product_id,updated_at,analyzed_at) VALUES(?,?,?,?,0)",(str(media_group_id),u["id"],pid,time.time()))
                created_new=True
        else:
            cur=c.execute("""INSERT INTO products(user_id,name,category,condition,photo_url,photos_json,price,status,source,telegram_message_id)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",(u["id"],name,"","",url,json.dumps([url]),0,"draft","telegram",str(msg.get("message_id",""))))
            pid=cur.lastrowid
            c.execute("UPDATE products SET source_type=? WHERE id=?",((u["default_source"] if "default_source" in u.keys() and u["default_source"] else "home"),pid))
            if (u["default_source"] if "default_source" in u.keys() else "")=="lot" and ("active_lot_id" in u.keys()) and u["active_lot_id"]:
                c.execute("UPDATE products SET lot_id=? WHERE id=?",(u["active_lot_id"],pid))
            c.execute("UPDATE users SET pending_product_id=? WHERE id=?",(pid,u["id"]))
            created_new=True
        c.commit(); c.close()
        if created_new:
            event(u["id"],"PRODUCT_CREATED_FROM_TELEGRAM",pid)
            src=(u["default_source"] if "default_source" in u.keys() and u["default_source"] else "home")
            labels={"home":"🏠 Дом","lot":"📦 Лот","buy":"🛒 Закупка","business":"🏪 Остатки","friends":"👥 Знакомые"}
            telegram_send(chat_id,"Получил 💜 Фото сохранил. Сейчас сам посмотрю товар и соберу карточку. Источник: "+labels.get(src,"📦 Товар")+".")
        if media_group_id:
            threading.Thread(target=analyze_album_background,args=(str(media_group_id),pid,u["id"],chat_id),daemon=True).start()
        elif created_new:
            threading.Thread(target=analyze_single_background,args=(pid,u["id"],chat_id),daemon=True).start()
        return {"ok":True}
    telegram_send(chat_id,"Кидай фото товара или альбом 💜 Я сохраню их в магазин. Источник можно поменять командой /source.")
    return {"ok":True}

@app.post("/api/products")
async def add_product(
    authorization:Optional[str]=Header(None),
    name:str=Form("Новый товар"),
    category:str=Form(""),
    condition:str=Form(""),
    price:float=Form(0),
    photo:UploadFile|None=File(None)
):
    u=user(authorization)
    url=None
    if photo and photo.filename:
        raw=await photo.read()
        fn=secrets.token_hex(8)+".jpg"
        path=UPLOADS/fn
        try:
            img=Image.open(io.BytesIO(raw))
            img=ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((1800,1800))
            img.save(path,"JPEG",quality=90,optimize=True)
        except Exception:
            path.write_bytes(raw)
        url="/uploads/"+fn
    c=con()
    cur=c.execute(
        "INSERT INTO products(user_id,name,category,condition,photo_url,price) VALUES(?,?,?,?,?,?)",
        (u["id"],name,category,condition,url,price)
    )
    c.commit()
    pid=cur.lastrowid
    c.close()
    event(u["id"],"PRODUCT_CREATED",pid)
    return {"id":pid}

@app.get("/api/products/{pid}")
def product(pid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    p=c.execute("SELECT * FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
    c.close()
    if not p:
        raise HTTPException(404,"Товар не найден")
    return dict(p)

@app.post("/api/products/{pid}/analyze")
def analyze(pid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    data=analyze_product_ai(pid,u["id"],con,ask_ai,event,KOLYA_PROMPT)
    if not data:
        raise HTTPException(404,"Товар не найден")
    return {"data":data,"text":data.get("next_action") or "Готово 💜"}

@app.post("/api/products/{pid}/clarify")
def clarify_product(pid:int,d:Clarification,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    note=d.text.strip()
    if not note:
        raise HTTPException(400,"Напиши уточнение")
    c=con()
    p=c.execute("SELECT user_notes FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
    if not p:
        c.close()
        raise HTTPException(404,"Товар не найден")
    old=(p["user_notes"] or "").strip()
    merged=(old+"\n"+note).strip() if old else note
    c.execute("UPDATE products SET user_notes=?,ai_status='waiting' WHERE id=? AND user_id=?",(merged,pid,u["id"]))
    c.execute("UPDATE users SET pending_product_id=? WHERE id=?",(pid,u["id"]))
    c.commit(); c.close()
    data=analyze_product_ai(pid,u["id"],con,ask_ai,event,KOLYA_PROMPT)
    event(u["id"],"PRODUCT_CLARIFIED",pid,{"text":note})
    return {"ok":True,"data":data}

@app.post("/api/products/{pid}/purchase-cost")
def set_purchase_cost(pid:int,d:PurchaseCost,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    if d.buy_price<0:
        raise HTTPException(400,"Закупка не может быть отрицательной")
    c=con()
    p=c.execute("SELECT id,price FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
    if not p:
        c.close(); raise HTTPException(404,"Товар не найден")
    c.execute("UPDATE products SET buy_price=? WHERE id=? AND user_id=?",(d.buy_price,pid,u["id"]))
    c.commit(); c.close()
    event(u["id"],"PURCHASE_COST_SET",pid,{"buy_price":d.buy_price})
    return {"ok":True,"hint":resale_hint(p["price"],d.buy_price)}

@app.post("/api/products/{pid}/listing")
def listing(pid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    p=product(pid,authorization)
    ai={}
    try: ai=json.loads(p.get("ai_json") or "{}")
    except Exception: ai={}
    result=ask_ai(
        KOLYA_PROMPT+"\nСделай живое объявление без рекламных штампов и воды. Используй только подтверждённые данные из AI-карточки и полей товара. Не превращай неизвестное в факт. Верни ЗАГОЛОВОК и ОПИСАНИЕ.",
        "Карточка по фото: "+json.dumps(ai,ensure_ascii=False)+"\nТовар: "+p["name"]+"; категория: "+(p["category"] or "")+
        "; состояние: "+(p["condition"] or "")+"; цена: "+str(p["price"])
    )
    if not result:
        result="ЗАГОЛОВОК:\n"+p["name"]+" — "+(p["condition"] or "")+"\nОПИСАНИЕ:\nПродаю "+p["name"].lower()+". Состояние: "+(p["condition"] or "").lower()+". Перед публикацией добавьте точные размеры и комплект."
    title=p["name"]
    desc=result
    if "ОПИСАНИЕ:" in result:
        a,b=result.split("ОПИСАНИЕ:",1)
        title=a.replace("ЗАГОЛОВОК:","").strip()
        desc=b.strip()
    c=con()
    c.execute(
        "UPDATE products SET listing_title=?,listing_description=? WHERE id=? AND user_id=?",
        (title,desc,pid,u["id"])
    )
    c.commit()
    c.close()
    event(u["id"],"LISTING_GENERATED",pid)
    return {"title":title,"description":desc}

@app.post("/api/products/{pid}/boost")
def boost_product(pid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    p=product(pid,authorization)
    try:
        ai=json.loads(p.get("ai_json") or "{}")
    except Exception:
        ai={}
    days=0
    if p.get("listed_at"):
        try:
            days=max(0,(datetime.utcnow()-datetime.fromisoformat(str(p["listed_at"]).replace("Z",""))).days)
        except Exception:
            days=0
    c=con()
    history=[dict(x) for x in c.execute(
        "SELECT name,category,sold_price,buy_price FROM products WHERE user_id=? AND status='sold' ORDER BY id DESC LIMIT 12",
        (u["id"],)
    ).fetchall()]
    c.close()
    result=ask_ai(
        KOLYA_PROMPT+"""
Ты разбираешь товар, который уже выставлен, но продавец хочет понять, что докрутить.
У нас НЕТ данных о просмотрах, избранном и сообщениях с площадки — никогда не выдумывай их.
Оцени только то, что реально есть: фотографии/AI-карточка, заголовок, описание, цена, сколько дней товар в продаже и личная история продаж пользователя.
Ответь коротко в формате:
ГЛАВНОЕ: <1 главная мысль>
1. <конкретное действие>
2. <конкретное действие>
3. <только если действительно нужно>
ЦЕНА: <не снижать сразу / проверить / почему>
Не давай больше трёх действий. Не говори, что товар "плохой".""",
        "Товар: "+str(p.get("name") or "")+
        "\nДней в продаже: "+str(days)+
        "\nЦена: "+str(p.get("price") or 0)+
        "\nAI-карточка по фото: "+json.dumps(ai,ensure_ascii=False)+
        "\nЗаголовок: "+str(p.get("listing_title") or "")+
        "\nОписание: "+str(p.get("listing_description") or "")+
        "\nПрошлые продажи пользователя: "+json.dumps(history,ensure_ascii=False)
    )
    if not result:
        tips=[]
        if not p.get("listing_title"): tips.append("Сначала докрути заголовок — покупатель должен сразу понять, что продаётся.")
        if not p.get("listing_description"): tips.append("Добавь короткое описание с важными деталями и состоянием.")
        if ai.get("missing_photos"): tips.append("Досними то, чего не хватает по фото: "+", ".join(ai.get("missing_photos")[:2]))
        if not tips: tips=["Товар уже оформлен. Не режем цену вслепую: сначала обнови главное фото и проверь, всё ли важное видно покупателю."]
        result="ГЛАВНОЕ: Есть за что зацепиться — сначала докрутим подачу.\n1. "+tips[0]
        if len(tips)>1: result+="\n2. "+tips[1]
        result+="\nЦЕНА: Не снижай сразу без данных о реакции покупателей."
    event(u["id"],"PRODUCT_BOOST_ANALYZED",pid,{"days":days})
    return {"text":result,"days":days}

@app.post("/api/products/{pid}/listed")
def listed(pid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    c.execute("UPDATE products SET status='listed',listed_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(pid,u["id"]))
    c.commit()
    c.close()
    event(u["id"],"PRODUCT_LISTED",pid)
    return {"ok":True}

@app.post("/api/products/{pid}/sold")
def sold(pid:int,d:Sale,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    current=c.execute("SELECT buy_price FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
    stored_buy=float(current["buy_price"] or 0) if current else 0
    final_buy=d.buy_price if d.buy_price>0 else stored_buy
    c.execute(
        "UPDATE products SET status='sold',sold_price=?,buy_price=?,sale_expenses=?,sold_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (d.sold_price,final_buy,d.expenses,pid,u["id"])
    )
    c.commit()
    c.close()
    event(u["id"],"PRODUCT_SOLD",pid,{"sold_price":d.sold_price})
    return {"ok":True}

@app.post("/api/chat")
def chat(d:Chat,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    sold=[dict(x) for x in c.execute(
        "SELECT name,category,sold_price FROM products WHERE user_id=? AND status='sold' ORDER BY id DESC LIMIT 12",
        (u["id"],)
    ).fetchall()]
    active=[dict(x) for x in c.execute(
        "SELECT name,category,price,status FROM products WHERE user_id=? AND status!='sold' ORDER BY id DESC LIMIT 12",
        (u["id"],)
    ).fetchall()]
    cat_rows=[dict(x) for x in c.execute(
        """SELECT category,
                  SUM(CASE WHEN status='sold' THEN 1 ELSE 0 END) sold_count,
                  SUM(CASE WHEN status='listed' THEN 1 ELSE 0 END) listed_count,
                  SUM(CASE WHEN status='sold' THEN sold_price ELSE 0 END) revenue
           FROM products WHERE user_id=? GROUP BY category ORDER BY sold_count DESC LIMIT 10""",
        (u["id"],)
    ).fetchall()]
    c.close()
    context={"goal":u["goal"],"sales":sold,"active":active,"category_stats":cat_rows}
    result=ask_ai(
        KOLYA_PROMPT+"\nТы отвечаешь как личный помощник именно этого продавца. Используй его историю товаров и продаж, не придумывай факты.",
        "Контекст: "+json.dumps(context,ensure_ascii=False)+"\nВопрос: "+d.text
    )
    if not result:
        q=d.text.lower()
        if not sold:
            result="Продаж пока нет. Добавь товар и доведи его до продажи — после этого я начну использовать твою историю."
        else:
            stop={"что","мы","уже","продавали","похожее","похожий","похожая","похожие","сколько","как","моя","мой","моё","мои","товар","за","и","на"}
            words=[w.strip(".,!?;:()[]{}\"'").lower() for w in q.split()]
            words=[w for w in words if len(w)>=4 and w not in stop]
            matches=[]
            for s in sold:
                hay=((s.get("name") or "")+" "+(s.get("category") or "")).lower()
                if any(w in hay for w in words):
                    matches.append(s)
            chosen=matches if matches else sold[:3]
            parts=[]
            for s in chosen[:3]:
                parts.append((s.get("name") or "Товар")+" — "+str(int(s.get("sold_price") or 0))+" ₽")
            if matches:
                result="Да, у тебя уже были похожие продажи: "+("; ".join(parts))+". Это реальные цены из твоей истории."
            else:
                result="В истории у тебя сейчас: "+("; ".join(parts))+". Если добавишь похожий товар, я смогу опираться на эти продажи."
    return {"text":result}
