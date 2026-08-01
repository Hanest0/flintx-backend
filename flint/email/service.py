"""
Flint — Transactional Email Service
Uses Resend (resend.com) — free tier covers 3,000 emails/month.
All templates are plain HTML — intentionally simple and fast to load.
"""

import os
import httpx

RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL      = os.getenv("FROM_EMAIL", "noreply@flintx.tv")
FRONTEND_URL    = os.getenv("FRONTEND_URL", "https://flintx.tv")


def _send(to: str, subject: str, html: str):
    """Send an email via Resend. Silently logs failures — never crashes the app."""
    if not RESEND_API_KEY:
        print(f"[EMAIL SKIPPED — no RESEND_API_KEY] To: {to} | Subject: {subject}")
        return
    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")


def _base(content: str) -> str:
    return f"""
    <div style="background:#0A0A0A;padding:40px 20px;font-family:Inter,sans-serif;min-height:100vh;">
      <div style="max-width:520px;margin:0 auto;">
        <div style="margin-bottom:32px;">
          <span style="font-size:28px;font-weight:900;color:#ffffff;letter-spacing:-1px;">
            🔥 fli<span style="color:#FF5C00;">nt</span>
          </span>
        </div>
        <div style="background:#1A1A1A;border:1px solid #2A2A2A;border-radius:12px;padding:32px;">
          {content}
        </div>
        <div style="margin-top:24px;font-size:12px;color:#6A6A7A;text-align:center;">
          FlintX · flintx.tv · If you didn't request this, you can safely ignore this email.
        </div>
      </div>
    </div>
    """


def send_verification_email(to: str, name: str, token: str):
    url = f"{FRONTEND_URL}/verify-email?token={token}"
    _send(to, "Verify your FlintX email address", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Welcome to FlintX, {name.split()[0]} 🔥
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 24px;">
            One click to verify your email and activate your account.
        </p>
        <a href="{url}" style="display:inline-block;background:linear-gradient(135deg,#FF5C00,#FF9E2C);
           color:#ffffff;text-decoration:none;padding:13px 28px;border-radius:8px;
           font-weight:700;font-size:15px;">
            Verify my email →
        </a>
        <p style="color:#6A6A7A;font-size:12px;margin:20px 0 0;">
            Link expires in 48 hours. If the button doesn't work, copy this URL:<br/>
            <span style="color:#FF9E2C;word-break:break-all;">{url}</span>
        </p>
    """))


def send_welcome_email(to: str, name: str, role: str):
    role_msg = {
        "creator":    "Your Script Writer, Voice Generator, and Revenue Predictor are ready. Upload your first video and start earning 80% of ad revenue.",
        "viewer":     "Browse thousands of videos and consider joining FlintX Pass to earn credits while watching.",
        "both":       "You have full creator and viewer access. Upload videos, earn ad revenue, and earn credits watching too.",
        "advertiser": "Your advertiser account is under review. We'll email you within 24 hours once approved.",
    }.get(role, "Your account is ready.")

    _send(to, "You're on FlintXX 🔥", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            You're live on FlintX 🔥
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 20px;">
            {role_msg}
        </p>
        <a href="{FRONTEND_URL}" style="display:inline-block;background:linear-gradient(135deg,#FF5C00,#FF9E2C);
           color:#ffffff;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:700;font-size:15px;">
            Open Flint →
        </a>
    """))


def send_password_reset_email(to: str, name: str, token: str):
    url = f"{FRONTEND_URL}/reset-password?token={token}"
    _send(to, "Reset your FlintX password", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Password reset
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 24px;">
            Someone requested a password reset for your FlintX account. If that was you, click below.
            Link expires in 2 hours.
        </p>
        <a href="{url}" style="display:inline-block;background:linear-gradient(135deg,#FF5C00,#FF9E2C);
           color:#ffffff;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:700;font-size:15px;">
            Reset password →
        </a>
    """))


def send_payout_confirmed_email(to: str, name: str, amount_pence: int, method: str):
    amount = CurrencyService.format(amount_pence, "USD")
    _send(to, f"Your Flint payout of {amount} is on its way", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Payout sent 💸
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 20px;">
            <strong style="color:#00E5A0;">{amount}</strong> is being sent to your {method} account.
            It typically arrives within 1 business day.
        </p>
        <p style="color:#6A6A7A;font-size:12px;">
            Next payout: 1st or 15th of next month. Minimum threshold: £50.
        </p>
    """))


def send_video_approved_email(to: str, name: str, video_title: str):
    _send(to, "Your video is live on FlintX 🔥", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Your video is live
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 12px;">
            <strong style="color:#ffffff;">{video_title}</strong><br/>
            Passed moderation and is now visible to Flint viewers worldwide.
            Ad revenue tracking is active — earnings appear in your wallet within 24 hours of your first ad view.
        </p>
        <a href="{FRONTEND_URL}" style="display:inline-block;background:linear-gradient(135deg,#FF5C00,#FF9E2C);
           color:#ffffff;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:700;font-size:15px;">
            View your video →
        </a>
    """))


def send_video_rejected_email(to: str, name: str, video_title: str, reason: str):
    _send(to, "Update needed on your Flint video", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Video needs attention
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 12px;">
            <strong style="color:#ffffff;">{video_title}</strong><br/>
            Was not approved during moderation for the following reason:
        </p>
        <div style="background:#0A0A0A;border:1px solid #FF4455;border-radius:8px;padding:14px;margin:0 0 20px;">
            <p style="color:#FF4455;font-size:14px;margin:0;">{reason}</p>
        </div>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 20px;">
            You have <strong style="color:#ffffff;">one appeal</strong>. Log in to Flint, find the video,
            and submit your appeal with supporting context. We review all appeals within 48 hours.
        </p>
        <a href="{FRONTEND_URL}/studio" style="display:inline-block;background:#1A1A1A;border:1px solid #2A2A2A;
           color:#F0EBE0;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:700;font-size:15px;">
            Go to Studio →
        </a>
    """))


def send_advertiser_approved_email(to: str, company: str):
    _send(to, "Your Flint advertiser account is approved", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Welcome to FlintXX Ads 📢
        </h2>
        <p style="color:#9A9080;font-size:14px;line-height:1.7;margin:0 0 20px;">
            <strong style="color:#ffffff;">{company}</strong> is approved to advertise on FlintX.
            Log in to upload your creative, set your targeting, and go live.
        </p>
        <a href="{FRONTEND_URL}/advertiser" style="display:inline-block;background:linear-gradient(135deg,#FF5C00,#FF9E2C);
           color:#ffffff;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:700;font-size:15px;">
            Open Advertiser Dashboard →
        </a>
    """))


def send_monthly_report_email(to: str, company: str, month: str, stats: dict):
    _send(to, f"FlintX Advertising Report — {month}", _base(f"""
        <h2 style="color:#ffffff;font-size:22px;font-weight:800;margin:0 0 8px;">
            Monthly Report — {month}
        </h2>
        <p style="color:#9A9080;font-size:13px;margin:0 0 20px;">{company}</p>
        <table style="width:100%;border-collapse:collapse;">
          {"".join(f'<tr><td style="padding:10px 0;border-bottom:1px solid #2A2A2A;color:#9A9080;font-size:13px;">{k}</td><td style="padding:10px 0;border-bottom:1px solid #2A2A2A;color:#F0EBE0;font-size:14px;font-weight:700;text-align:right;">{v}</td></tr>' for k,v in stats.items())}
        </table>
        <p style="color:#6A6A7A;font-size:12px;margin:20px 0 0;">
            Full interactive report available in your Flint Advertiser Dashboard.
        </p>
    """))
