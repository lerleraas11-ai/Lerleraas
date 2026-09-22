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
DB=BASE/"app.db"
UPLOADS=BASE/"uploads"; UPLOADS.mkdir(exist_ok=True)

app=FastAPI(title="Бизнес из дома — Авито-помощник")
app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")
app.mount("/uploads",StaticFiles(directory=UPLOADS),name="uploads")

def con():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
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
    """)
    ucols={r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    for col,ddl in [
      ("telegram_user_id","ALTER TABLE users ADD COLUMN telegram_user_id TEXT"),
      ("telegram_chat_id","ALTER TABLE users ADD COLUMN telegram_chat_id TEXT"),
      ("telegram_link_token","ALTER TABLE users ADD COLUMN telegram_link_token TEXT")
    ]:
        if col not in ucols: c.execute(ddl)
    pcols={r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    for col,ddl in [
      ("photos_json","ALTER TABLE products ADD COLUMN photos_json TEXT DEFAULT '[]'"),
      ("source","ALTER TABLE products ADD COLUMN source TEXT DEFAULT 'web'"),
      ("telegram_message_id","ALTER TABLE products ADD COLUMN telegram_message_id TEXT")
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
            path=BASE/image_url.lstrip("/")
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

class Chat(BaseModel):
    text:str

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
    c.close()
    sold=[p for p in ps if p["status"]=="sold"]
    earned=sum(p["sold_price"] or 0 for p in sold)
    profit=sum((p["sold_price"] or 0)-(p["buy_price"] or 0) for p in sold)
    return {
        "profile":{"name":u["name"],"goal":u["goal"]},
        "products":ps,
        "stats":{"earned":earned,"profit":profit,"sold":len(sold),"avg":earned/len(sold) if sold else 0}
    }


@app.get("/api/telegram/status")
def telegram_status(authorization:Optional[str]=Header(None)):
    u=user(authorization)
    username=os.getenv("TELEGRAM_BOT_USERNAME","").lstrip("@")
    return {"connected":bool(u.get("telegram_user_id")),"bot_username":username}

@app.post("/api/telegram/link")
def telegram_link(authorization:Optional[str]=Header(None)):
    u=user(authorization)
    username=os.getenv("TELEGRAM_BOT_USERNAME","").lstrip("@")
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
            telegram_send(chat_id,"Ссылка устарела. Открой Авито-помощник и нажми «Подключить Telegram» ещё раз.")
            return {"ok":True}
        c.execute("UPDATE users SET telegram_user_id=?,telegram_chat_id=?,telegram_link_token=NULL WHERE id=?",(tg_user_id,chat_id,u["id"]))
        c.commit(); c.close()
        event(u["id"],"TELEGRAM_CONNECTED")
        telegram_send(chat_id,"Готово 💜 Telegram связан с твоим Авито-помощником. Теперь просто присылай сюда фото товара или альбом из нескольких фото.")
        return {"ok":True}
    photos=msg.get("photo") or []
    if photos:
        c=con()
        u=c.execute("SELECT * FROM users WHERE telegram_user_id=?",(tg_user_id,)).fetchone()
        if not u:
            c.close()
            telegram_send(chat_id,"Сначала свяжи Telegram с аккаунтом через кнопку в Авито-помощнике 💜")
            return {"ok":True}
        url=telegram_download_photo(photos[-1]["file_id"])
        caption=(msg.get("caption") or "").strip()
        name=caption.splitlines()[0][:120] if caption else "Товар из Telegram"
        media_group_id=msg.get("media_group_id")
        pid=None
        if media_group_id:
            grp=c.execute("SELECT * FROM telegram_groups WHERE media_group_id=?",(str(media_group_id),)).fetchone()
            if grp:
                pid=grp["product_id"]
                row=c.execute("SELECT photos_json,photo_url FROM products WHERE id=? AND user_id=?",(pid,u["id"])).fetchone()
                arr=[]
                try: arr=json.loads(row["photos_json"] or "[]")
                except Exception: pass
                if not arr and row["photo_url"]: arr=[row["photo_url"]]
                arr.append(url)
                c.execute("UPDATE products SET photos_json=? WHERE id=?",(json.dumps(arr),pid))
            else:
                arr=[url]
                cur=c.execute("""INSERT INTO products(user_id,name,category,condition,photo_url,photos_json,price,status,source,telegram_message_id)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",(u["id"],name,"","",url,json.dumps(arr),0,"draft","telegram",str(msg.get("message_id",""))))
                pid=cur.lastrowid
                c.execute("INSERT INTO telegram_groups(media_group_id,user_id,product_id) VALUES(?,?,?)",(str(media_group_id),u["id"],pid))
                event(u["id"],"PRODUCT_CREATED_FROM_TELEGRAM",pid)
        else:
            cur=c.execute("""INSERT INTO products(user_id,name,category,condition,photo_url,photos_json,price,status,source,telegram_message_id)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",(u["id"],name,"","",url,json.dumps([url]),0,"draft","telegram",str(msg.get("message_id",""))))
            pid=cur.lastrowid
            event(u["id"],"PRODUCT_CREATED_FROM_TELEGRAM",pid)
        c.commit(); c.close()
        telegram_send(chat_id,"Фото получила 💜 Товар уже появился в Авито-помощнике. Открой приложение — там можно разобрать его с AI и сделать объявление.")
        return {"ok":True}
    telegram_send(chat_id,"Пришли фото товара или альбом из нескольких фото. Я перенесу их в Авито-помощник 💜")
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
        """Ты AI-помощник продавца. Анализируй фото товара точно и не выдумывай характеристики.
Начни ответ строго так:
НАЗВАНИЕ: <короткое название>
КАТЕГОРИЯ: <категория>
РАЗБОР: <что важно покупателю, что видно, что уточнить>.
Если чего-то не видно — так и скажи.""",
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
        "Пиши естественное объявление без рекламных штампов. Не придумывай характеристики. Верни ЗАГОЛОВОК и ОПИСАНИЕ.",
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
    c.execute("UPDATE products SET status='listed' WHERE id=? AND user_id=?",(pid,u["id"]))
    c.commit()
    c.close()
    event(u["id"],"PRODUCT_LISTED",pid)
    return {"ok":True}

@app.post("/api/products/{pid}/sold")
def sold(pid:int,d:Sale,authorization:Optional[str]=Header(None)):
    u=user(authorization)
    c=con()
    c.execute(
        "UPDATE products SET status='sold',sold_price=?,buy_price=?,sold_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (d.sold_price,d.buy_price,pid,u["id"])
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
        "Ты персональный AI-помощник проекта «Бизнес из дома». Используй только факты из контекста. Дай 1–3 действия.",
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
