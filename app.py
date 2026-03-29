import os, asyncio, edge_tts, requests, base64, json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

AUDIO_DIR = os.path.join(os.path.dirname(__file__), 'static', 'audio')
os.makedirs(AUDIO_DIR, exist_ok=True)

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# قائمة الأصوات المتاحة
VOICES = {
    "ar-EG-SalmaNeural":   "سلمى (مصري)",
    "ar-SA-ZariyahNeural": "زارية (سعودي)",
    "ar-IQ-BasselNeural":  "باسل (عراقي)",
}

# سجل المحادثة لكل جلسة
chat_history = []
settings = {
    "voice": "ar-EG-SalmaNeural",
    "speed": "+0%",
    "volume": "+0%",
}


def build_system_prompt(mode: str) -> str:
    if mode == "read":
        return (
            "أنت محرك قراءة للمكفوفين. مهمتك الوحيدة: اقرأ كل النص الموجود في الصورة كما هو، "
            "مع تشكيل كامل لكل الكلمات، بدون أي تعليق أو إضافة. "
            "إن لم يوجد نص، قل: لا يوجد نص في الصورة."
        )
    elif mode == "describe":
        return (
            "أنت مساعد للمكفوفين. صف ما تراه في الصورة بدقة وتفصيل: الأشخاص، الأشياء، الألوان، المكان. "
            "استخدم لغة واضحة وبسيطة، مع تشكيل كامل، بدون علامات ماركداون."
        )
    else:  # chat
        return (
            "أنت مساعد ذكي متخصص لمساعدة المكفوفين. أجب بوضوح وإيجاز وتشكيل كامل. "
            "بدون علامات ماركداون. إذا أُرسلت صورة، استفد منها في الإجابة. "
            "كن صبوراً ومتفهماً واستخدم لغة عربية فصيحة سهلة."
        )


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/get_audio')
def get_audio():
    fn = request.args.get('fn', '')
    fp = os.path.join(AUDIO_DIR, os.path.basename(fn))
    if not os.path.exists(fp):
        return jsonify({'error': 'file not found'}), 404
    return send_file(fp, mimetype="audio/mpeg")


@app.route('/process', methods=['POST'])
def process():
    global chat_history, settings
    try:
        mode       = request.form.get('mode', 'chat')
        user_query = request.form.get('query', '').strip()
        img_file   = request.files.get('image')

        # ── ردود محلية سريعة ──────────────────────────────────────────
        local_keywords = {
            "وقت": lambda: datetime.now().strftime("%I:%M %p").replace("AM","صباحاً").replace("PM","مساءً"),
            "ساعة": lambda: datetime.now().strftime("السَّاعَةُ الآنَ %I:%M %p").replace("AM","صَبَاحاً").replace("PM","مَسَاءً"),
            "تاريخ": lambda: datetime.now().strftime("اليَوْمُ %d-%m-%Y"),
            "يوم":  lambda: ["الأَحَد","الاثنين","الثُّلاثاء","الأَرْبِعَاء","الخَمِيس","الجُمُعَة","السَّبْت"][datetime.now().weekday() % 7 if datetime.now().weekday() != 6 else 6],
        }
        for kw, fn_local in local_keywords.items():
            if kw in user_query:
                res_text = fn_local()
                return _audio_response(res_text)

        # ── إعادة تعيين المحادثة عند وضع القراءة والوصف ──────────────
        sys_msg = build_system_prompt(mode)
        if mode in ("read", "describe"):
            chat_history = [{"role": "system", "content": sys_msg}]
        elif not chat_history:
            chat_history = [{"role": "system", "content": sys_msg}]

        # ── بناء محتوى الرسالة ────────────────────────────────────────
        content = []
        if user_query:
            content.append({"type": "text", "text": user_query})
        if img_file:
            img_bytes = img_file.read()
            img_b64   = base64.b64encode(img_bytes).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
            })
        if not content:
            return jsonify({'error': 'لا يوجد محتوى'}), 400

        chat_history.append({"role": "user", "content": content})

        # ── استدعاء OpenRouter / Gemini ───────────────────────────────
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "google/gemini-2.0-flash-001",
                "messages": chat_history,
                "temperature": 0.1 if mode == "read" else 0.35,
                "max_tokens": 1500,
            },
            timeout=60
        ).json()

        if "error" in resp:
            raise Exception(resp["error"].get("message", "خطأ من الخادم"))

        res_text = resp['choices'][0]['message']['content'].strip()
        # تنظيف علامات ماركداون المتبقية
        for md in ['**', '__', '##', '#', '```', '`', '*', '_']:
            res_text = res_text.replace(md, '')

        chat_history.append({"role": "assistant", "content": res_text})

        # حد الذاكرة: احتفظ بآخر 20 رسالة + رسالة النظام
        if len(chat_history) > 21:
            chat_history = [chat_history[0]] + chat_history[-20:]

        return _audio_response(res_text)

    except Exception as e:
        err_msg = f"حدث خطأ: {str(e)}"
        return _audio_response(err_msg, error=True)


def _audio_response(text: str, error=False):
    """يولّد ملف صوتي ويعيد JSON بالنتيجة."""
    global settings
    try:
        fname = f"{os.urandom(4).hex()}.mp3"
        fpath = os.path.join(AUDIO_DIR, fname)
        voice = settings.get("voice", "ar-EG-SalmaNeural")
        rate  = settings.get("speed", "+0%")

        async def _gen():
            comm = edge_tts.Communicate(text, voice, rate=rate)
            await comm.save(fpath)

        asyncio.run(_gen())
        return jsonify({
            'text': text,
            'audio_url': f'/get_audio?fn={fname}',
            'error': error
        })
    except Exception as e:
        return jsonify({'text': text, 'error': True, 'detail': str(e)}), 500


@app.route('/reset', methods=['POST'])
def reset():
    global chat_history
    chat_history = []
    return jsonify({'status': 'cleared'})


@app.route('/settings', methods=['GET', 'POST'])
def handle_settings():
    global settings
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        settings.update({k: v for k, v in data.items() if k in settings})
        return jsonify({'status': 'ok', 'settings': settings})
    return jsonify(settings)


@app.route('/voices')
def get_voices():
    return jsonify(VOICES)


# تنظيف الملفات الصوتية القديمة (أكثر من 50 ملف)
@app.route('/cleanup', methods=['POST'])
def cleanup():
    try:
        files = sorted(
            [os.path.join(AUDIO_DIR, f) for f in os.listdir(AUDIO_DIR) if f.endswith('.mp3')],
            key=os.path.getmtime
        )
        removed = 0
        while len(files) > 50:
            os.remove(files.pop(0))
            removed += 1
        return jsonify({'removed': removed})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
