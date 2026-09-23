import json, re

def parse_json_answer(text):
    if not text:
        return None
    s=text.strip()
    fence=chr(96)*3
    if s.startswith(fence):
        s=re.sub(r'^'+re.escape(fence)+r'(?:json)?\s*','',s)
        s=re.sub(r'\s*'+re.escape(fence)+r'$','',s)
    try:
        return json.loads(s)
    except Exception:
        m=re.search(r'\{.*\}',s,re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

def product_photos(product):
    arr=[]
    try:
        arr=json.loads(product.get('photos_json') or '[]')
    except Exception:
        arr=[]
    if not arr and product.get('photo_url'):
        arr=[product['photo_url']]
    return [x for x in arr if isinstance(x,str) and x.startswith('/uploads/')][:8]

def analyze_product_ai(pid, uid, con, ask_ai, event, kolya_prompt):
    c=con()
    row=c.execute('SELECT * FROM products WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not row:
        c.close()
        return None
    p=dict(row)
    history=[dict(x) for x in c.execute("SELECT name,category,sold_price,buy_price FROM products WHERE user_id=? AND status='sold' ORDER BY id DESC LIMIT 12",(uid,)).fetchall()]
    c.execute("UPDATE products SET ai_status='thinking' WHERE id=?",(pid,))
    c.commit()
    c.close()
    photos=product_photos(p)
    instructions=kolya_prompt+'''
Ты разбираешь товар только по фотографиям и подписи. Все фотографии относятся к одному товару.
Верни только валидный JSON без markdown:
{
  "name":"короткое точное название",
  "category":"категория",
  "brand":null,
  "model":null,
  "color":null,
  "material":null,
  "size":null,
  "condition":"что реально видно",
  "visible_details":[],
  "defects":[],
  "included":[],
  "needs_clarification":[],
  "buyer_value":"за что покупателю зацепиться",
  "suggested_price_low":0,
  "suggested_price_high":0,
  "price_note":"почему такой ориентир",
  "photo_advice":"что улучшить в главном фото",
  "next_action":"один следующий шаг"
}
Правила: не угадывай бренд, модель, размер, материал, состояние, комплект или дефекты. Если не видно — null или пустой список.
Размер можно назвать только если он читается на бирке/маркировке. Материал — только если прочитан на бирке или очевиден без сомнений.
Цена — предварительный ориентир. Если данных недостаточно и нет надёжной истории пользователя, ставь 0 и прямо пиши, что цену нужно уточнить.
'''
    prompt='Подпись пользователя: '+str(p.get('name') or '')+'\nИсточник товара: '+str(p.get('source_type') or 'home')+'\nИстория продаж пользователя: '+json.dumps(history,ensure_ascii=False)
    result=ask_ai(instructions,prompt,image_urls=photos)
    data=parse_json_answer(result)
    if not data:
        data={
          'name':p.get('name') or 'Товар','category':p.get('category') or '',
          'brand':None,'model':None,'color':None,'material':None,'size':None,
          'condition':p.get('condition') or '','visible_details':[],'defects':[],'included':[],
          'needs_clarification':['AI-разбор пока недоступен — проверь подключение AI'],
          'buyer_value':'','suggested_price_low':0,'suggested_price_high':0,
          'price_note':'Цена не определена','photo_advice':'','next_action':'Проверь подключение AI и повтори разбор.'
        }
    name=str(data.get('name') or p.get('name') or 'Товар')[:160]
    category=str(data.get('category') or p.get('category') or '')[:120]
    condition=str(data.get('condition') or p.get('condition') or '')[:300]
    try:
        low=float(data.get('suggested_price_low') or 0)
        high=float(data.get('suggested_price_high') or 0)
    except Exception:
        low=high=0
    suggested=(low+high)/2 if low>0 and high>0 else (high or low or 0)
    c=con()
    if suggested>0 and not (p.get('price') or 0):
        c.execute("UPDATE products SET name=?,category=?,condition=?,price=?,ai_json=?,ai_status='ready',ai_updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(name,category,condition,suggested,json.dumps(data,ensure_ascii=False),pid,uid))
    else:
        c.execute("UPDATE products SET name=?,category=?,condition=?,ai_json=?,ai_status='ready',ai_updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(name,category,condition,json.dumps(data,ensure_ascii=False),pid,uid))
    c.commit()
    c.close()
    event(uid,'PRODUCT_AI_READY',pid)
    return data