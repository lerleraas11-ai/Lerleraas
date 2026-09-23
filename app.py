import os, sqlite3, secrets, hashlib, hmac, json, io, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image, ImageOps

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
    """)
    ucols={r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    for col,ddl in [
      ("telegram_user_id","ALTER TABLE users ADD COLUMN telegram_user_id TEXT"),
      ("telegram_chat_id","ALTER TABLE users ADD COLUMN telegram_chat_id TEXT"),
      ("telegram_link_token","ALTER TABLE users ADD COLUMN telegram_link_token TEXT"),
      ("default_source","ALTER TABLE users ADD COLUMN default_source TEXT DEFAULT 'home'"),
      ("business_modes","ALTER TABLE users ADD COLUMN business_modes TEXT DEFAULT '[]'"),
      ("salary_percent","ALTER TABLE users ADD COLUMN salary_percent REAL DEFAULT 30"),
      ("goal_label","ALTER TABLE users ADD COLUMN goal_label TEXT DEFAULT ''")
    ]:
        if col not in ucols: c.execute(ddl)
    pcols={r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    for col,ddl in [
      ("photos_json","ALTER TABLE products ADD COLUMN photos_json TEXT DEFAULT '[]'"),
      ("source","ALTER TABLE products ADD COLUMN source TEXT DEFAULT 'web'"),
      ("telegram_message_id","ALTER TABLE products ADD COLUMN telegram_message_id TEXT"),
      ("source_type","ALTER TABLE products ADD COLUMN source_type TEXT DEFAULT 'home'"),
      ("sale_expenses","ALTER TABLE products ADD COLUMN sale_expenses REAL DEFAULT 0"),
      ("listed_at","ALTER TABLE products ADD COLUMN listed_at TEXT")
    ]:
        if col not in pcols: c.execute(ddl)
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

def ask_ai(instructions,prompt,image_url=None):
    cli=ai_client()
    if not cli:
        return None
    try:
        content=[{"type":"input_text","text":prompt}]
        if image_url and image_url.startswith("/uploads/"):
            path=UPLOADS/Path(image_url).name
            if path.exists():
                import base64, mimetypes
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

def telegram_send(chat_id,text):
    try:
        telegram_api("sendMessage",{"chat_id":chat_id,"text":text})
    except Exception:
        pass

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
    payload={"url":public_url+"/telegram/webhook","allowed_updates":["message"]}
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

@app.get("/")
def home():
    return FileResponse(BASE/"static"/"index.html")

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
    c.close()
    sold=[p for p in ps if p["status"]=="sold"]
    revenue=sum(p["sold_price"] or 0 for p in sold)
    profit=sum((p["sold_price"] or 0)-(p["buy_price"] or 0)-(p.get("sale_expenses") or 0) for p in sold)
    counts={
      "new":sum(1 for p in ps if p["status"]=="draft"),
      "listed":sum(1 for p in ps if p["status"]=="listed"),
      "sold":len(sold)
    }
    by_source={}
    for p in sold:
        key=p.get("source_type") or "home"
        row=by_source.setdefault(key,{"revenue":0,"profit":0,"sold":0})
        row["revenue"]+=(p["sold_price"] or 0)
        row["profit"]+=(p["sold_price"] or 0)-(p["buy_price"] or 0)-(p.get("sale_expenses") or 0)
        row["sold"]+=1
    try: modes=json.loads(u.get("business_modes") or "[]")
    except Exception: modes=[]
    salary_percent=float(u.get("salary_percent") or 30)
    salary_target=max(profit,0)*salary_percent/100
    return {
        "profile":{
          "name":u["name"],"goal":u["goal"],"goal_label":u.get("goal_label") or "",
          "default_source":u.get("default_source") or "home","business_modes":modes,
          "salary_percent":salary_percent
        },
        "products":ps,
        "withdrawals":withdrawals,
        "stats":{
          "revenue":revenue,"earned":revenue,"profit":profit,"sold":len(sold),
          "avg":revenue/len(sold) if sold else 0,"counts":counts,"by_source":by_source,
          "salary_taken":taken,"salary_target":salary_target,"salary_available":max(salary_target-taken,0)
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
        telegram_send(chat_id,"Готово 💜 Telegram связан с Колей AI. Теперь просто присылай сюда фото товара или альбом из нескольких фото.")
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
                c.execute("UPDATE products SET photos_json=? WHERE id=?",(json.dumps(arr),pid))
            else:
                arr=[url]
                cur=c.execute("""INSERT INTO products(user_id,name,category,condition,photo_url,photos_json,price,status,source,telegram_message_id)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",(u["id"],name,"","",url,json.dumps(arr),0,"draft","telegram",str(msg.get("message_id",""))))
                pid=cur.lastrowid
                c.execute("UPDATE products SET source_type=? WHERE id=?",((u["default_source"] if "default_source" in u.keys() and u["default_source"] else "home"),pid))
                c.execute("INSERT INTO telegram_groups(media_group_id,user_id,product_id) VALUES(?,?,?)",(str(media_group_id),u["id"],pid))
                created_new=True
        else:
            cur=c.execute("""INSERT INTO products(user_id,name,category,condition,photo_url,photos_json,price,status,source,telegram_message_id)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",(u["id"],name,"","",url,json.dumps([url]),0,"draft","telegram",str(msg.get("message_id",""))))
            pid=cur.lastrowid
            c.execute("UPDATE products SET source_type=? WHERE id=?",((u["default_source"] if "default_source" in u.keys() and u["default_source"] else "home"),pid))
            created_new=True
        c.commit(); c.close()
        if created_new:
            event(u["id"],"PRODUCT_CREATED_FROM_TELEGRAM",pid)
        telegram_send(chat_id,"Фото получила 💜 Товар уже появился в помощнике продаж. Открой приложение — там можно разобрать его с AI и сделать объявление.")
        return {"ok":True}
    telegram_send(chat_id,"Пришли фото товара или альбом из нескольких фото. Я перенесу их в помощник продаж 💜")
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
    p=product(pid,authorization)
    c=con()
    history=[dict(x) for x in c.execute(
        "SELECT name,sold_price FROM products WHERE user_id=? AND status='sold' ORDER BY id DESC LIMIT 8",
        (u["id"],)
    ).fetchall()]
    c.close()
    result=ask_ai(
        KOLYA_PROMPT+"""
Проанализируй фото товара.
Начни ответ строго так:
НАЗВАНИЕ: <короткое название>
КАТЕГОРИЯ: <категория>
РАЗБОР: <коротко: что видно, что важно покупателю, что уточнить и что сделать сейчас>.
Не выдумывай то, чего не видно.""",
        "Текущая подпись: "+p["name"]+"; категория: "+(p["category"] or "")+
        "; состояние: "+(p["condition"] or "")+
        "; прошлые продажи: "+json.dumps(history,ensure_ascii=False),
        p.get("photo_url")
    )
    if result and "НАЗВАНИЕ:" in result:
        lines=result.splitlines()
        new_name=None; new_cat=None
        for line in lines[:4]:
            if line.startswith("НАЗВАНИЕ:"): new_name=line.split(":",1)[1].strip()
            if line.startswith("КАТЕГОРИЯ:"): new_cat=line.split(":",1)[1].strip()
        if new_name or new_cat:
            c=con()
            if new_name: c.execute("UPDATE products SET name=? WHERE id=? AND user_id=?",(new_name,pid,u["id"]))
            if new_cat: c.execute("UPDATE products SET category=? WHERE id=? AND user_id=?",(new_cat,pid,u["id"]))
            c.commit(); c.close()
    if not result:
        result="Демо-разбор: "+p["name"]+". Проверь размер, маркировку, комплект и дефекты. Чем точнее карточка, тем проще покупателю принять решение."
    event(u["id"],"PRODUCT_ANALYZED",pid)
    return {"text":result}

@app.post("/api/products/{pid}/listing")
def listing(pid:int,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    p=product(pid,authorization)
    result=ask_ai(
        KOLYA_PROMPT+"\nСделай живое объявление без рекламных штампов и воды. Не придумывай характеристики. Верни ЗАГОЛОВОК и ОПИСАНИЕ.",
        "Товар: "+p["name"]+"; категория: "+(p["category"] or "")+
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
    c.execute(
        "UPDATE products SET status='sold',sold_price=?,buy_price=?,sale_expenses=?,sold_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (d.sold_price,d.buy_price,d.expenses,pid,u["id"])
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
    c.close()
    context={"goal":u["goal"],"sales":sold,"active":active}
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
