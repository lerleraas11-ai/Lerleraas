import os, sqlite3, secrets, hashlib, hmac, json, io
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
    """)
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

def ask_ai(instructions,prompt):
    cli=ai_client()
    if not cli:
        return None
    try:
        r=cli.responses.create(
            model=os.getenv("OPENAI_MODEL","gpt-5.6-luna"),
            instructions=instructions,
            input=prompt
        )
        return r.output_text
    except Exception:
        return None

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
        "Ты AI-помощник продавца. Не выдумывай характеристики. Отвечай коротко и практично.",
        "Товар: "+p["name"]+"; категория: "+(p["category"] or "")+
        "; состояние: "+(p["condition"] or "")+
        "; прошлые продажи: "+json.dumps(history,ensure_ascii=False)+
        ". Что важно покупателю и что уточнить?"
    )
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
        result=("Продаж пока нет. Добавь товар и доведи его до продажи — после этого я начну использовать твою историю."
                if not sold else
                "У тебя уже "+str(len(sold))+" продаж. Я вижу их историю и могу помогать сравнивать товары.")
    return {"text":result}
