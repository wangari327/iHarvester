"""Small, dependency-free client portal for a single shared promotion."""
# ruff: noqa: E501

from __future__ import annotations

from html import escape
from typing import Any

from app.utils.time import as_utc, utcnow


def _number(value: Any) -> int:
    return int(value or 0)


def _percent(value: int, total: int) -> int:
    return 0 if total <= 0 else max(0, min(100, round(100 * value / total)))


def _iso(value: Any) -> str | None:
    return as_utc(value).isoformat() if value else None


def campaign_progress_payload(
    campaign: dict[str, Any],
    totals: dict[str, Any],
    cycle_stats: dict[str, Any],
    metrics: dict[str, Any],
    *,
    live_posts: int,
    joined: int,
) -> dict[str, Any]:
    """Return public aggregate stats without exposing the channel network."""
    pending = sum(_number(totals.get(status)) for status in ("PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED"))
    sent = _number(totals.get("SENT"))
    failed = _number(totals.get("FAILED_PERMANENT"))
    unknown = _number(totals.get("UNKNOWN_SEND_STATE"))
    cleaned = _number(totals.get("CLEANED"))
    cleanup_failed = _number(totals.get("CLEANUP_FAILED"))
    cancelled = _number(totals.get("CANCELLED"))
    delivery_total = sum(_number(totals.get(status)) for status in (
        "PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED", "SENT", "FAILED_PERMANENT", "UNKNOWN_SEND_STATE", "CLEANED", "CLEANUP_FAILED", "CANCELLED",
    ))
    delivery_complete = delivery_total - pending
    start = campaign.get("start_at_utc")
    end = campaign.get("current_end_at_utc")
    now = as_utc(campaign.get("archived_at")) if campaign.get("archived_at") else utcnow()
    elapsed_percent = 0
    if start and end:
        total_seconds = max(1, int((as_utc(end) - as_utc(start)).total_seconds()))
        elapsed_seconds = max(0, min(int((now - as_utc(start)).total_seconds()), total_seconds))
        elapsed_percent = _percent(elapsed_seconds, total_seconds)
    status = str(campaign.get("status") or "UNKNOWN")
    return {
        "campaign": {
            "name": str(campaign.get("name") or "Promotion"),
            "status": status.lower().replace("_", " ").title(),
            "mode": str(campaign.get("mode") or "STANDARD").lower().replace("_", " ").title(),
            "start_at": _iso(start),
            "end_at": _iso(end),
            "updated_at": _iso(metrics.get("last_updated_at") or campaign.get("updated_at")),
        },
        "delivery": {
            "complete": delivery_complete,
            "total": delivery_total,
            "percent": _percent(delivery_complete, delivery_total),
            "sent": sent,
            "pending": pending,
            "failed": failed,
            "unknown": unknown,
            "cancelled": cancelled,
        },
        "timeline": {"percent": elapsed_percent, "status": status},
        "cycles": {"completed": _number(cycle_stats.get("completed")), "planned": _number(cycle_stats.get("planned"))},
        "cleanup": {"deleted": cleaned, "failed": cleanup_failed, "live_posts": max(0, int(live_posts))},
        "engagement": {"tracked_joins": max(0, int(joined))},
    }


def render_client_portal(token: str, payload: dict[str, Any]) -> str:
    """Render one self-contained page; browser polling supplies live changes."""
    title = escape(payload["campaign"]["name"])
    initial = _safe_json(payload)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · iHarvester</title>
<style>
:root{{color-scheme:dark;--bg:#101923;--card:#182534;--line:#2a3a4d;--muted:#a8b7c7;--text:#f5f8fb;--accent:#36c275;--warn:#efb54e;}}
*{{box-sizing:border-box}} body{{margin:0;background:linear-gradient(135deg,#0c1420,#101d2b);color:var(--text);font:16px system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:780px;margin:0 auto;padding:24px 16px 48px}}h1{{margin:0 0 4px;font-size:28px}}h2{{font-size:18px;margin:0 0 12px}}p,.muted{{color:var(--muted)}}.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:16px;box-shadow:0 12px 30px #0003}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}@media(max-width:520px){{.grid{{grid-template-columns:1fr}}}}
.metric{{padding:12px;border:1px solid var(--line);border-radius:12px}}.metric b{{display:block;font-size:22px;margin-top:4px}}.track{{height:12px;border-radius:99px;background:#0c1420;overflow:hidden;margin:10px 0 4px}}.fill{{height:100%;background:linear-gradient(90deg,#26a85f,#55e494);transition:width .4s}}.amber{{background:linear-gradient(90deg,#b97920,#f4c661)}}
label{{display:block;margin:12px 0 6px;font-weight:600}}input,textarea,select,button{{font:inherit}}input,textarea,select{{width:100%;padding:10px;border-radius:10px;border:1px solid var(--line);background:#101923;color:var(--text)}}textarea{{min-height:92px;resize:vertical}}button,.button{{display:inline-block;border:0;border-radius:10px;padding:11px 14px;background:var(--accent);color:#062014;font-weight:750;text-decoration:none;cursor:pointer;margin-top:14px}}button.secondary{{background:#30445a;color:var(--text)}}.notice{{display:none;margin-top:12px;padding:12px;border-radius:10px;background:#113628;color:#d5ffe6}}small{{color:var(--muted)}}
</style></head><body><main>
<header><small>iHarvester client progress</small><h1 id="campaign-name">{title}</h1><p id="campaign-state">Live campaign information refreshes automatically.</p></header>
<section class="card"><h2>Campaign progress</h2><div class="track"><div id="delivery-bar" class="fill"></div></div><b id="delivery-label">Loading…</b><p class="muted" id="delivery-detail"></p><div class="track"><div id="timeline-bar" class="fill amber"></div></div><b id="timeline-label">Campaign time</b><p class="muted" id="timeline-detail"></p></section>
<section class="card"><div class="grid"><div class="metric"><small>Delivered</small><b id="sent">—</b></div><div class="metric"><small>Awaiting / processing</small><b id="pending">—</b></div><div class="metric"><small>Delivery issues</small><b id="failed">—</b></div><div class="metric"><small>Posts currently live</small><b id="live-posts">—</b></div><div class="metric"><small>Cycles completed</small><b id="cycles">—</b></div><div class="metric"><small>Tracked joins</small><b id="joins">—</b></div></div><p class="muted"><small id="updated">Updates while this page is open; no manual refresh is needed.</small></p></section>
<section class="card"><h2>Request a promotion</h2><p class="muted">Requests are never sent automatically. Your campaign manager reviews payment and approves every request.</p>
<form id="request-form"><label>Request type</label><select id="request-type"><option value="RERUN">Run this same promotion again</option><option value="NEW">Request a new promotion</option></select><label>Your name or reference</label><input id="client-name" maxlength="100" required placeholder="For example: Acme Media"><label>Preferred start time</label><input id="desired-start" type="datetime-local"><small>Leave blank if you want the manager to choose the time.</small><label>Notes</label><textarea id="details" maxlength="3000" placeholder="Timing, changes, or instructions"></textarea><button type="submit">Send request</button></form><div id="notice" class="notice"></div></section>
</main><script>
const token={_safe_json(token)}; const progressUrl=`/client/c/${{encodeURIComponent(token)}}/progress`; const requestUrl=`/client/c/${{encodeURIComponent(token)}}/requests`;
let current={initial};
const n=v=>Number(v||0).toLocaleString(); const when=v=>v?new Date(v).toLocaleString():"Not scheduled";
function paint(data){{current=data;const c=data.campaign,d=data.delivery,t=data.timeline,cy=data.cycles,cl=data.cleanup,e=data.engagement;
document.title=`${{c.name}} · iHarvester`;document.getElementById('campaign-name').textContent=c.name;document.getElementById('campaign-state').textContent=`${{c.status}} · ${{c.mode}}`;
document.getElementById('delivery-bar').style.width=`${{d.percent}}%`;document.getElementById('delivery-label').textContent=d.total?`${{d.percent}}% of delivery jobs complete`:'Waiting for the first delivery cycle';document.getElementById('delivery-detail').textContent=`${{n(d.sent)}} delivered · ${{n(d.pending)}} in progress · ${{n(d.failed+d.unknown)}} issue(s)`;
document.getElementById('timeline-bar').style.width=`${{t.percent}}%`;document.getElementById('timeline-label').textContent=`Campaign time: ${{t.percent}}%`;document.getElementById('timeline-detail').textContent=`${{when(c.start_at)}} → ${{when(c.end_at)}}`;
document.getElementById('sent').textContent=n(d.sent);document.getElementById('pending').textContent=n(d.pending);document.getElementById('failed').textContent=n(d.failed+d.unknown);document.getElementById('live-posts').textContent=n(cl.live_posts);document.getElementById('cycles').textContent=`${{n(cy.completed)}} / ${{n(cy.planned)}}`;document.getElementById('joins').textContent=n(e.tracked_joins);document.getElementById('updated').textContent=`Last data update: ${{when(c.updated_at)}} · refreshes automatically every 10 seconds.`;}}
async function refresh(){{try{{const r=await fetch(progressUrl,{{cache:'no-store'}});if(!r.ok)throw Error();paint(await r.json())}}catch{{document.getElementById('updated').textContent='Live updates are temporarily unavailable. Retrying automatically…'}}}}
paint(current);setInterval(refresh,10000);
document.getElementById('request-form').addEventListener('submit',async event=>{{event.preventDefault();const notice=document.getElementById('notice');notice.style.display='block';notice.textContent='Sending request…';const wanted=document.getElementById('desired-start').value;const body={{request_type:document.getElementById('request-type').value,client_name:document.getElementById('client-name').value,details:document.getElementById('details').value,desired_start:wanted||null,client_timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'}};try{{const r=await fetch(requestUrl,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});const out=await r.json();if(!r.ok)throw Error(out.detail||'Could not send request');notice.textContent=out.material_url? 'Request recorded. Send your post materials through Telegram, then tap Done there so it reaches approval.': 'Request recorded and waiting for payment/approval.';if(out.material_url){{const a=document.createElement('a');a.className='button secondary';a.href=out.material_url;a.textContent='Send materials in Telegram';notice.appendChild(document.createElement('br'));notice.appendChild(a)}}event.target.reset()}}catch(error){{notice.textContent=error.message||'Could not send request. Please try again.'}}}});
</script></body></html>"""


def _safe_json(value: Any) -> str:
    """A tiny JSON encoder safe to embed inside a script tag."""
    import json

    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
