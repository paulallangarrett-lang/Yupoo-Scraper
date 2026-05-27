# Yupoo Browser

A mobile-first PWA to browse public Yupoo seller catalogs.  
Install it on your Android home screen like a real app.

---

## Deploy to Railway (free, ~2 min)

1. Go to **https://railway.app** and sign in with GitHub
2. Click **"New Project" → "Deploy from GitHub repo"**
3. Upload or push this folder to a GitHub repo, then select it
4. Railway auto-detects Python + Procfile and deploys
5. Once live, click **"Generate Domain"** to get your public URL

**Or use Railway CLI:**
```bash
npm install -g @railway/cli
railway login
railway init
railway up
railway domain
```

---

## Deploy to Render (free alternative)

1. Go to **https://render.com** → New → Web Service
2. Connect your GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`
5. Choose free tier → Deploy

> ⚠️ Render free tier sleeps after 15 min inactivity (first load is slow).  
> Railway stays awake.

---

## Add to Android Home Screen (PWA)

1. Open your hosted URL in **Chrome** on Android
2. Tap the **3-dot menu (⋮)**
3. Tap **"Add to Home screen"**
4. Name it **"Yupoo"** → tap Add
5. It opens like a full-screen app with no browser UI

---

## Usage

- Enter a seller URL or username: `summer-original` or `https://summer-original.x.yupoo.com`
- Browse their categories and albums
- Filter albums by name with the search bar
- Tap any album to see all images
- Tap an image to open the full-size viewer
- Swipe left/right in the viewer to navigate
- Tap ⬇ to open the original full-resolution image

---

## Run locally

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```
