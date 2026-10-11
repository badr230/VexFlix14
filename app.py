import os, json, secrets, time, traceback
from pathlib import Path
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
import engine

BASE=Path(__file__).resolve().parent
OUT=BASE/'output'; OUT.mkdir(exist_ok=True)
app=Flask(__name__)
app.secret_key=os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH']=50*1024*1024
engine.BASE=BASE
engine.OUT=OUT
engine.HISTORY_FILE=BASE/'posted.json'
engine.load_env()

def cfg(): return engine.load_config()
def envfile():
    return engine.read_env_file()
def save_env(d): engine.write_env_file(d); engine.load_env()
def connected():
    e=envfile()
    return bool(e.get('FB_PAGE_ID') and e.get('FB_PAGE_TOKEN'))
def login_required(fn):
    @wraps(fn)
    def inner(*a,**kw):
        password=os.environ.get('DASHBOARD_PASSWORD') or envfile().get('DASHBOARD_PASSWORD')
        if password and session.get('authed') is not True: return redirect(url_for('login'))
        return fn(*a,**kw)
    return inner

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        password=os.environ.get('DASHBOARD_PASSWORD') or envfile().get('DASHBOARD_PASSWORD')
        if password and secrets.compare_digest(request.form.get('password',''),password):
            session['authed']=True; return redirect(url_for('index'))
        flash('كلمة المرور غير صحيحة','error')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    c=cfg(); e=envfile()
    history=engine.load_history()
    recent=sorted(OUT.glob('*'),key=lambda p:p.stat().st_mtime,reverse=True)[:12]
    return render_template('index.html',c=c,connected=connected(),has_ai=bool(e.get('GEMINI_API_KEY') or e.get('ANTHROPIC_API_KEY')),history=history[-10:][::-1],files=[p.name for p in recent if p.is_file()],ffmpeg=engine.have_ffmpeg())

@app.route('/settings',methods=['GET','POST'])
@login_required
def settings():
    c=cfg(); e=envfile()
    if request.method=='POST':
        c['page_name']=request.form.get('page_name','').strip() or c['page_name']
        c['watermark_text']=request.form.get('watermark_text','').strip() or c['page_name']
        c['dialect']=request.form.get('dialect','').strip() or c['dialect']
        c['ai_provider']=request.form.get('ai_provider','gemini')
        c['gemini_model']=request.form.get('gemini_model','gemini-2.5-flash').strip()
        c['schedule']['posts_per_day']=max(1,min(8,int(request.form.get('posts_per_day',3))))
        c['schedule']['days_ahead']=max(1,min(7,int(request.form.get('days_ahead',3))))
        for k in ('reel','image','text'): c['daily_plan'][k]=max(0,min(8,int(request.form.get(k,0))))
        (BASE/'config.json').write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf8')
        for k in ('GEMINI_API_KEY','ANTHROPIC_API_KEY','FB_APP_ID','FB_APP_SECRET','FB_PAGE_ID','FB_PAGE_TOKEN','DASHBOARD_PASSWORD'):
            v=request.form.get(k,'').strip()
            if v: e[k]=v
        save_env(e); flash('تم حفظ الإعدادات. القيم السرية لا تظهر بعد الحفظ.','ok')
        return redirect(url_for('settings'))
    return render_template('settings.html',c=c,e=e,connected=connected())
@app.route('/connect-facebook',methods=['POST'])
@login_required
def connect_facebook():
    e=envfile()
    token=request.form.get('token','').strip()
    app_id=request.form.get('app_id','').strip() or e.get('FB_APP_ID','')
    app_secret=request.form.get('app_secret','').strip() or e.get('FB_APP_SECRET','')
    if not token or not app_id or not app_secret:
        flash('أدخل User Access Token وApp ID وApp Secret.','error'); return redirect(url_for('settings'))
    try:
        r=engine.requests.get(f'{engine.GRAPH}/oauth/access_token',params={'grant_type':'fb_exchange_token','client_id':app_id,'client_secret':app_secret,'fb_exchange_token':token},timeout=30)
        data=engine._fail(r); long_token=data['access_token']
        r=engine.requests.get(f'{engine.GRAPH}/me/accounts',params={'access_token':long_token,'limit':100},timeout=30)
        pages=engine._fail(r).get('data',[])
        if not pages:
            me=engine._fail(engine.requests.get(f'{engine.GRAPH}/me',params={'access_token':long_token,'fields':'id,name'},timeout=30))
            pages=[{'id':me['id'],'name':me.get('name',''),'access_token':long_token}]
        session['fb_pages']=[{'id':p['id'],'name':p.get('name',''),'token':p['access_token']} for p in pages]
        session['fb_app_id']=app_id; session['fb_app_secret']=app_secret
        flash('اختر الصفحة التي تريد ربطها.','ok')
    except Exception as ex: flash('تعذر الربط: '+str(ex)[:350],'error')
    return redirect(url_for('settings'))
@app.route('/select-page',methods=['POST'])
@login_required
def select_page():
    pages=session.get('fb_pages',[]); pid=request.form.get('page_id')
    p=next((x for x in pages if x['id']==pid),None)
    if not p: flash('اختر صفحة صحيحة.','error')
    else:
        e=envfile(); e.update(FB_PAGE_ID=p['id'],FB_PAGE_TOKEN=p['token'],FB_APP_ID=session.get('fb_app_id',''),FB_APP_SECRET=session.get('fb_app_secret',''))
        save_env(e); flash('تم ربط الصفحة: '+p['name'],'ok'); session.pop('fb_pages',None)
    return redirect(url_for('settings'))

@app.route('/generate',methods=['POST'])
@login_required
def generate():
    c=cfg(); kind=request.form.get('kind','image'); theme=request.form.get('theme','').strip() or c['themes'][0]
    if kind not in ('image','text','reel'): flash('نوع منشور غير صالح','error'); return redirect(url_for('index'))
    try:
        recent=[x.get('text','') for x in engine.load_history() if isinstance(x,dict)]
        text,caption=engine.generate_text(c,theme,recent,kind)
        stamp=int(time.time()); media=''
        if kind=='image':
            media=f'post-{stamp}.jpg'; engine.make_image(c,text,str(OUT/media),theme)
        elif kind=='reel':
            if not engine.have_ffmpeg(): raise RuntimeError('FFmpeg غير متوفر على الاستضافة؛ اختر صورة أو كتابة أو ثبّت FFmpeg.')
            media=f'reel-{stamp}.mp4'; engine.make_video(c,text,str(OUT/media),theme)
        item={'id':stamp,'kind':kind,'theme':theme,'text':text,'caption':caption,'media':media,'created_at':datetime.utcnow().isoformat()+'Z','status':'draft'}
        drafts=engine.load_history(); drafts.append(item); engine.save_history(drafts)
        session['last_draft']=stamp
        flash('تم إنشاء مسودة. راجع النص قبل النشر.','ok')
    except Exception as ex: flash('فشل التوليد: '+str(ex)[:450],'error')
    return redirect(url_for('draft',draft_id=session.get('last_draft',0)))
@app.route('/draft/<int:draft_id>')
@login_required
def draft(draft_id):
    items=engine.load_history(); item=next((x for x in items if x.get('id')==draft_id),None)
    if not item: flash('المسودة غير موجودة','error'); return redirect(url_for('index'))
    return render_template('draft.html',item=item)
@app.route('/publish/<int:draft_id>',methods=['POST'])
@login_required
def publish(draft_id):
    if not connected(): flash('اربط صفحة فيسبوك أولاً من الإعدادات.','error'); return redirect(url_for('settings'))
    items=engine.load_history(); item=next((x for x in items if x.get('id')==draft_id),None)
    if not item: flash('المسودة غير موجودة','error'); return redirect(url_for('index'))
    if item.get('status')=='published': flash('هذه المسودة منشورة بالفعل.','error'); return redirect(url_for('draft',draft_id=draft_id))
    try:
        result=engine.publish(item['kind'],str(OUT/item['media']) if item.get('media') else None,item['text'],item['caption'])
        item['status']='published'; item['facebook_result']=result
        engine.save_history(items); flash('تم إرسال المنشور إلى فيسبوك.','ok')
    except Exception as ex: flash('فشل النشر: '+str(ex)[:450],'error')
    return redirect(url_for('draft',draft_id=draft_id))
@app.route('/schedule',methods=['POST'])
@login_required
def schedule():
    if not connected(): flash('اربط صفحة فيسبوك أولاً.','error'); return redirect(url_for('settings'))
    c=cfg()
    try:
        plan=engine.build_plan(c,engine.have_ffmpeg())
        if not plan: raise RuntimeError('لا توجد مواعيد مستقبلية؛ راجع أوقات الجدولة.')
        recent=[x.get('text','') for x in engine.load_history() if isinstance(x,dict)]
        created=[]
        for slot,kind in plan:
            theme=__import__('random').choice(c['themes'])
            txt,cap=engine.generate_text(c,theme,recent,kind)
            media=''
            if kind=='image':
                media=f'scheduled-{int(slot.timestamp())}.jpg'; engine.make_image(c,txt,str(OUT/media),theme)
            elif kind=='reel':
                if not engine.have_ffmpeg(): kind='image'; media=f'scheduled-{int(slot.timestamp())}.jpg'; engine.make_image(c,txt,str(OUT/media),theme)
                else:
                    media=f'scheduled-{int(slot.timestamp())}.mp4'; engine.make_video(c,txt,str(OUT/media),theme)
            result=engine.publish(kind,str(OUT/media) if media else None,txt,cap,slot.timestamp())
            created.append({'id':int(time.time()*1000)%2147483647,'kind':kind,'theme':theme,'text':txt,'caption':cap,'media':media,'created_at':datetime.utcnow().isoformat()+'Z','status':'scheduled','scheduled_for':slot.isoformat(),'facebook_result':result})
            recent.append(txt); time.sleep(max(0,int(c.get('ai_delay_seconds',2))))
        engine.save_history(engine.load_history()+created)
        flash(f'تم إرسال {len(created)} منشوراً للجدولة على فيسبوك. تأكد من المواعيد في Meta Business Suite.','ok')
    except Exception as ex: flash('توقفت الجدولة: '+str(ex)[:450]+'. قد تكون بعض المنشورات السابقة قد جُدولت بالفعل؛ تحقق من Meta Business Suite قبل إعادة المحاولة.','error')
    return redirect(url_for('index'))
@app.route('/media/<path:name>')
@login_required
def media(name): return send_from_directory(OUT,name)
@app.route('/health')
def health(): return {'ok':True,'service':'Facebook Content Studio'}
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
