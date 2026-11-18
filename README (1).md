# Test bot (aiogram v3) — O'zbekcha

## 1. Token olish (BotFather)
1. Telegramda **@BotFather** ni oching → `/newbot`.
2. Bot nomi va `...bot` bilan tugaydigan username bering.
3. Berilgan **token**ni nusxalang. (Eski token oshkor bo'lgan bo'lsa: `/revoke` → botni tanlang → yangi token.)

## 2. Admin ID olish
**@userinfobot** ga `/start` yuboring — u sizning ID raqamingizni ko'rsatadi.

## 3. Sozlash
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```
`.env` faylini ochib `BOT_TOKEN` va `ADMIN_IDS` ni to'ldiring.

## 4. Ishga tushirish
```bash
python bot.py
```
Baza (`quiz.db`) avtomatik yaratiladi.

## 5. Foydalanish
- Admin: `/admin` → «Yangi test yaratish» → nom, tavsif, vaqt, o'tish balli, savollar → kod beriladi.
- Foydalanuvchi: kodni yuboradi yoki «Testlar ro'yxati»dan tanlaydi. `/stop` — testni to'xtatish.

## Eslatma
Faol test sessiyalari xotirada saqlanadi: bot qayta ishga tushsa, jarayondagi testlar to'xtaydi (natijalar bazada qoladi).
Doimiy ishlashi uchun serverda `systemd` yoki `pm2`/`supervisor` ishlatish tavsiya etiladi.
