# FlintX Backend

Complete FastAPI backend for the FlintX video platform.

**43 database tables · 12 modules · 100+ API routes · All amounts in USD**

---

## Architecture

```
flintx-backend/
├── flint/
│   ├── main.py                 ← FastAPI app entry point
│   ├── database/
│   │   ├── models.py           ← Core tables (users, videos, transactions)
│   │   └── connection.py       ← DB session + table creation
│   ├── auth/                   ← Signup, login, JWT, email verify, reset
│   ├── videos/                 ← Upload, feed, view tracking, ad revenue
│   ├── studio/                 ← Script Writer, Voice Gen, Opportunity AI, Revenue Predictor
│   ├── wallet/                 ← Balance, transactions, payout requests
│   ├── moderation/             ← Auto (Rekognition+OpenAI) + human review + appeals
│   ├── payments/               ← PayPal webhooks, Wise payouts, batch processing
│   ├── advertiser/             ← Applications, campaigns, CPM billing, reporting
│   ├── algorithm/              ← Recommendation scorer + ad matching
│   ├── currency/               ← 44 currencies, 66 niches, live exchange rates
│   ├── workspace/              ← Multi-channel, Audience Bridge, Content Passport, Collab Split
│   ├── referral/               ← Referral flywheel, badge programme, external use tracking
│   ├── livestream/             ← RTMP/HLS streaming, chat, tips, subs, VOD
│   ├── payouts/                ← Phased payout system (Phase 1→4), milestone tracker
│   ├── storage/                ← S3 presigned URLs, MediaConvert, CloudFront
│   └── email/                  ← Transactional emails via Resend
├── requirements.txt
├── .env.example
└── README.md
```

---

## Local Setup (15 minutes)

### Step 1 — Install

```bash
cd flintx-backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2 — Configure

```bash
cp .env.example .env
```

**Minimum to run locally (SQLite, no external services):**
```env
ENVIRONMENT=development
FRONTEND_URL=http://localhost:3000
DATABASE_URL=sqlite:///./flintx_local.db
SECRET_KEY=any-random-string-at-least-32-characters-long
```

That's it. Everything else degrades gracefully — AI tools return demo responses,
emails print to console, video upload returns a fake URL.

### Step 3 — Run

```bash
uvicorn flint.main:app --reload --port 8000
```

- API: `http://localhost:8000`
- Interactive docs: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`

### Step 4 — Test

```bash
# Signup
curl -X POST http://localhost:8000/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"kai@test.com","password":"Test1234!","full_name":"Kai Davis","role":"creator"}'

# Activate manually (skips email in dev)
python3 -c "
from flint.database.connection import SessionLocal
from flint.database.models import User, AccountStatus
db = SessionLocal()
u = db.query(User).filter(User.email=='kai@test.com').first()
u.email_verified = True
u.status = AccountStatus.active
db.commit()
print('Activated:', u.email)
"

# Login
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"kai@test.com","password":"Test1234!"}'
# Copy the access_token from the response

# Test Studio (no API keys needed)
curl -X POST "http://localhost:8000/api/studio/predict?currency=USD" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"niche":"Tax & Accounting","subscribers":50000,"duration_min":12,"uploads_week":2,"quality":"good"}'

# Platform phase (public)
curl http://localhost:8000/api/payouts/phase

# All 66 niches with CPM
curl "http://localhost:8000/api/currencies/niches?currency=USD"
```

---

## API Reference

### Auth
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/signup` | Create account (creator/viewer/advertiser) |
| POST | `/api/auth/login` | Login → access_token + refresh_token |
| GET | `/api/auth/me` | Current user profile |
| POST | `/api/auth/refresh` | Refresh access token |
| POST | `/api/auth/verify-email` | Verify email with token |
| POST | `/api/auth/forgot-password` | Send reset email |
| POST | `/api/auth/reset-password` | Reset with token |

### Videos
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/videos/feed` | Personalised home feed |
| GET | `/api/videos/shorts` | Clips/shorts feed |
| GET | `/api/videos/{id}` | Single video detail |
| POST | `/api/videos/upload-url` | Get S3 presigned upload URL |
| POST | `/api/videos/process` | Trigger MediaConvert transcoding |
| POST | `/api/videos/{id}/view` | Record view + fire ad revenue |
| GET | `/api/videos/creator/{id}` | Creator's published videos |

### Studio
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/studio/script` | AI script writer (OpenAI) |
| POST | `/api/studio/voice` | Voice generator (ElevenLabs) |
| POST | `/api/studio/opportunity` | Trending topic finder |
| POST | `/api/studio/predict` | Revenue predictor (all 66 niches, 44 currencies) |
| GET | `/api/studio/plan` | Current Studio plan |

### Wallet & Payouts
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/wallet/balance` | Wallet balance |
| GET | `/api/wallet/transactions` | Transaction history |
| GET | `/api/payouts/phase` | Current platform phase + milestone tracker (public) |
| GET | `/api/payouts/my-earnings` | Creator earnings by phase |
| GET | `/api/payouts/my-credits` | Credit ledger detail |
| POST | `/api/payouts/request` | Request cash payout (phase-gated) |
| GET | `/api/payouts/founding` | Founding creator status + 10% bonus |
| GET | `/api/payouts/milestones` | Phase transition history (public) |

### Live Streaming
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/live/setup` | Create stream channel + get RTMP key |
| GET | `/api/live/channel` | My stream channel + OBS setup details |
| PATCH | `/api/live/channel` | Update channel settings |
| POST | `/api/live/channel/rotate-key` | Regenerate stream key |
| POST | `/api/live/go-live` | Start a stream |
| POST | `/api/live/end-stream` | End stream (saves VOD) |
| GET | `/api/live/directory` | All live streams by category |
| GET | `/api/live/{id}/chat` | Chat messages (poll) |
| POST | `/api/live/{id}/chat` | Send chat message |
| POST | `/api/live/{id}/tip` | Tip a creator |
| POST | `/api/live/{id}/sub` | Subscribe to creator channel |
| GET | `/api/live/quality/mine` | Creator Quality Score + ad eligibility |

### Workspace
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/workspace` | Create workspace |
| GET | `/api/workspace` | My workspace + all channels |
| GET | `/api/workspace/analytics` | Unified multi-channel analytics |
| POST | `/api/workspace/channels` | Create a channel |
| GET | `/api/workspace/channels` | List all channels |
| POST | `/api/workspace/bridge` | Create Audience Bridge campaign |
| POST | `/api/workspace/bridge/{id}/send` | Send bridge to subscribers |
| POST | `/api/workspace/passport` | Distribute video to multiple channels |
| POST | `/api/workspace/collab` | Propose revenue split with another creator |
| POST | `/api/workspace/collab/{id}/accept` | Accept collab split |
| POST | `/api/workspace/lend` | Offer Channel Lending |

### Referral & Badge
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/referral/link` | Create referral link |
| GET | `/api/referral/link` | My referral link + stats |
| POST | `/api/referral/click/{code}` | Record link click |
| GET | `/api/referral/referrals` | My referred users + earnings |
| POST | `/api/referral/badge` | Enrol in badge programme |
| GET | `/api/referral/badge` | My badges + credits |
| GET | `/api/referral/external/quota` | External Studio use quota |

### Niches & Currency
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/currencies/niches` | All 66 niches with CPM in any currency |
| GET | `/api/currencies/rates` | Live exchange rates (44 currencies) |
| POST | `/api/currencies/convert` | Convert amount between currencies |
| GET | `/api/currencies/cpm` | CPM by niche in any currency |

### Advertiser
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/advertiser/apply` | Advertiser application ($500 min budget) |
| GET | `/api/advertiser/dashboard` | Campaign performance |
| POST | `/api/advertiser/campaigns` | Create ad campaign |
| GET | `/api/advertiser/report` | Impression + spend report |

### Admin (require admin role)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/moderation/queue` | Human review queue |
| POST | `/api/moderation/approve/{id}` | Approve video |
| POST | `/api/moderation/reject/{id}` | Reject video |
| GET | `/api/payments/admin/pending` | Pending payout requests |
| POST | `/api/payments/admin/process/{id}` | Process payout via Wise |
| GET | `/api/payouts/admin/phase` | Full phase dashboard |
| GET | `/api/payouts/admin/liability` | Total locked credits liability |
| POST | `/api/payouts/admin/override` | Override platform phase |
| GET | `/api/live/admin/live` | All currently live streams |
| POST | `/api/referral/admin/badge/{id}/verify` | Verify creator badge |

---

## Phase System

FlintX uses a phased payout model to protect cash flow during launch.

| Phase | Collective Viewers | Creator Share | Cash Payouts | Min Payout |
|-------|-------------------|---------------|--------------|------------|
| 1 — Foundation | 0 – 25,000 | 40% (locked) | None | — |
| 2 — Momentum | 25,001 – 75,000 | 60% (50% cash) | $10.00 | $10 |
| 3 — Full Launch | 75,001 – 150,000 | 70% (full cash) | $30.00 | $30 |
| 4 — Standard | 150,001+ | 80% (full model) | $50.00 | $50 |

Collective viewer = unique registered account that watched 60+ seconds.
Phase transitions are automatic and logged publicly at `/api/payouts/milestones`.

---

## Feature Activation Order

Activate features in this order. Each builds on the previous.

**Week 1 — Core (no external accounts needed)**
1. Auth system: signup, login, JWT
2. Revenue predictor: works with zero API keys
3. Platform phase tracker: public, no auth
4. All 66 niches: `/api/currencies/niches`

**Week 2 — Communications**
5. Email: create Resend account (free), add `RESEND_API_KEY`
6. Email verification and password reset now work

**Week 3 — AI Tools**
7. OpenAI: `OPENAI_API_KEY` → Script Writer, Opportunity AI
8. ElevenLabs: `ELEVENLABS_API_KEY` → Voice Generator

**Week 4 — Video**
9. AWS: S3 bucket, MediaConvert role, CloudFront distribution
10. Video upload, transcoding, and playback

**Week 5 — Payments**
11. PayPal sandbox: Studio subscriptions + FlintX Pass
12. Wise sandbox: creator payout flow

**Week 6 — Live & Launch**
13. RTMP ingest: nginx-rtmp on server or AWS IVS
14. Supabase: switch from SQLite to PostgreSQL
15. Domain: point api.flintx.tv → your server

---

## Deployment (Railway — recommended, ~$24/mo)

```bash
# 1. Push to GitHub
git init && git add . && git commit -m "FlintX backend"
git remote add origin https://github.com/YOUR_USERNAME/flintx-backend
git push -u origin main

# 2. Create Railway project at railway.app
# → New Project → Deploy from GitHub → select repo
# → Add environment variables from .env (all production values)
# → Railway auto-detects Python and deploys

# 3. Add Supabase PostgreSQL
# → Supabase.com → New project → Settings → Database → Connection string
# → Set DATABASE_URL in Railway environment variables

# 4. Set custom domain
# → Railway → Settings → Domains → Add api.flintx.tv
# → Add CNAME record in Namecheap DNS

# 5. Test production
curl https://api.flintx.tv/health
```

## Deployment (DigitalOcean — $24/mo droplet)

```bash
# On Ubuntu 22.04 droplet
apt update && apt install -y python3.12 python3.12-venv python3-pip nginx certbot python3-certbot-nginx

# Clone and set up
git clone https://github.com/YOUR/flintx-backend /opt/flintx
cd /opt/flintx
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt gunicorn

# Configure
cp .env.example .env
nano .env  # add all production values

# Run as service
cat > /etc/systemd/system/flintx.service << EOF
[Unit]
Description=FlintX API
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/flintx
Environment="PATH=/opt/flintx/venv/bin"
ExecStart=/opt/flintx/venv/bin/gunicorn flint.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable flintx
systemctl start flintx

# Nginx config
cat > /etc/nginx/sites-available/flintx << EOF
server {
    server_name api.flintx.tv;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF
ln -s /etc/nginx/sites-available/flintx /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# SSL
certbot --nginx -d api.flintx.tv
```

---

## Infrastructure Cost Summary

| Service | Monthly | Notes |
|---------|---------|-------|
| Railway/DigitalOcean | $24 | Backend server |
| Supabase | $0 → $25 | Free to 500MB |
| Vercel (frontend) | $0 | Free tier |
| AWS S3 + CloudFront | $40 | Video storage + CDN |
| AWS MediaConvert | $15 | Transcoding |
| AWS Rekognition | $10 | Content moderation |
| OpenAI API | $30 | Script + moderation |
| ElevenLabs | $18 | Voice generation |
| Resend | $0 | Free 3K emails/mo |
| Domain | $4 | ~$50/yr amortised |
| **Total** | **$141/mo** | **Breakeven: 5 Studio subscribers** |
