import asyncio
import os
import base64
import datetime
import time
import json

from dotenv import load_dotenv
load_dotenv()

# Import nodriver under the common alias `uc`
import nodriver as uc

# OpenAI client for captcha solving
import openai

# Folder that will persist the Chromium/Chrome profile between runs.
# This keeps cookies & local storage so the session remains logged in.
PROFILE_DIR = os.path.join(os.path.dirname(__file__), "visa_profile")

# URL that shows appointment availability (or redirects to login if not authorised)
APPOINTMENT_URL = "https://www.usvisascheduling.com/schedule/?reschedule=true"

# URL that our in-page hook will ping when it detects available slots
# Will be constructed from credentials (telegram_bot_token/chat_id) or environment.
NOTIFY_URL = None

# Global retry configuration
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 2

_cred_path = os.path.join(os.path.dirname(__file__), "credential.json")
try:
    with open(_cred_path, "r", encoding="utf-8") as _f:
        _credential_data = json.load(_f)
except Exception as _e:
    print(f"[ERROR] Could not read credential.json – {_e}")
    _credential_data = {}

# Basic credential fields
USERNAME = _credential_data.get("username")
PASSWORD = _credential_data.get("password")

# Map security question input selectors (e.g., #kba1_response) to answers.
SECURITY_ANSWERS = {
    q.get("tag"): q.get("answer") for q in _credential_data.get("security_questions", []) if q.get("tag") and q.get("answer")
}

# Path to persist user's preferred date range for slot notifications
SLOT_PREFS_PATH = os.path.join(os.path.dirname(__file__), "slot_prefs.json")

# Build NOTIFY_URL from credentials or environment variables
if not NOTIFY_URL:
    tg_token = _credential_data.get('telegram_bot_token') or os.getenv('TELEGRAM_BOT_TOKEN')
    tg_chat = _credential_data.get('telegram_chat_id') or os.getenv('TELEGRAM_CHAT_ID')
    if tg_token and tg_chat:
        NOTIFY_URL = f"https://api.telegram.org/bot{tg_token}/sendMessage?chat_id={tg_chat}&text=hasSlot"
    else:
        # Keep a non-functional placeholder to avoid None checks later
        NOTIFY_URL = "https://api.telegram.org/bot<TELEGRAM_API_KEY>/sendMessage?chat_id=<CHAT_ID>&text=hasSlot"


def load_slot_prefs():
    """Load slot preferences from disk. Returns dict with 'start_date' and 'end_date' or None."""
    try:
        if os.path.exists(SLOT_PREFS_PATH):
            with open(SLOT_PREFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Return whatever is on disk. The presence of the file signals the
            # user's previous choice; empty strings mean 'no filtering'.
            return data
    except Exception as e:
        print(f"[WARN] Could not read slot prefs file – {e}")
    return None


def save_slot_prefs(prefs: dict):
    try:
        with open(SLOT_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
        print(f"[INFO] Saved slot preferences to {SLOT_PREFS_PATH}")
    except Exception as e:
        print(f"[WARN] Could not save slot prefs – {e}")



async def start_browser():
    """Launch a nodriver-controlled browser with a persistent profile."""
    if not os.path.exists(PROFILE_DIR):
        os.makedirs(PROFILE_DIR, exist_ok=True)

    cfg = uc.Config()
    # Persist profile so that cookies / localStorage survive across runs
    cfg.user_data_dir = PROFILE_DIR
    # Show the UI for now so we can see what's happening. Change to True if desired.
    cfg.headless = False

    browser = await uc.start(cfg)
    return browser


async def is_login_required(page) -> bool:
    """Best-effort heuristic to decide whether the current page is a login page."""
    try:
        current_url = page.url  # Works on recent nodriver versions
    except AttributeError:
        # Fallback: query location.href via JS evaluation
        current_url = await page.evaluate("location.href")

    # If we are on the Microsoft B2C login domain, we clearly need to log in.
    if current_url and "b2clogin.com" in current_url:
        return True

    # Additional heuristic: look for password input on the page.
    pwd_field = await page.select("input[type=password]")
    if pwd_field:
        return True

    return False


async def is_waiting_room(page) -> bool:
    """Return True if the current page is the high-traffic waiting room.

    The waiting room page shows an inline background image on the <body> tag
    that points to `waiting_room_background_en-US.png`. We inspect the body
    style attribute to look for that filename.
    """
    try:
        style_attr = await page.evaluate("document.body && document.body.getAttribute('style')")
        if style_attr and "waiting_room_background_en-US.png" in style_attr:
            return True
    except Exception:
        # In case DOM access fails (e.g. frame navigations), treat as not waiting room.
        pass
    return False


async def inject_fetch_hook(page):
    # Try to load persisted slot preferences from disk (the Python side will
    # embed them into the hook script). If not present, the variables will be
    # empty strings and the hook will behave as before (notify on any slot).
    prefs = load_slot_prefs() or {}
    start_date = prefs.get("start_date", "")
    end_date = prefs.get("end_date", "")

    hook_js = """
    (function() {
        if (window.__APPT_HOOK_INSTALLED__) return;  // Guard against duplicates
        window.__APPT_HOOK_INSTALLED__ = true;

        const TARGET_SUBSTR = '/custom-actions/?route=/api/v1/schedule-group/get-family-consular-schedule-days';
    const NOTIFY_URL   = '%s';
    const REFRESH_URL  = '%s';
    const START_DATE   = '%s';
    const END_DATE     = '%s';

        function analyseAvailability(json) {
            let hasSlots = false;
            try {
                try { console.log('[HOOK] ScheduleDays payload:', json && json.ScheduleDays); } catch(e) {}

                // If START_DATE and END_DATE are set, filter ScheduleDays to see if
                // any date falls within the inclusive range. Dates from the API are
                // in YYYY-MM-DD format so simple string -> Date parsing works.
                if (START_DATE && END_DATE && Array.isArray(json?.ScheduleDays)) {
                    const s = new Date(START_DATE + 'T00:00:00');
                    const e = new Date(END_DATE + 'T23:59:59');
                    const matches = json.ScheduleDays.filter(d => {
                        try {
                            const dt = new Date(d.Date + 'T00:00:00');
                            return dt >= s && dt <= e;
                        } catch (ex) { return false; }
                    });
                    hasSlots = matches.length > 0;
                    try { console.log('[HOOK] Matching dates in range:', matches); } catch(_) {}
                } else {
                    hasSlots = Array.isArray(json?.ScheduleDays) && json.ScheduleDays.length > 0;
                }
            } catch (e) {}

            if (hasSlots) {
                console.log('[HOOK] Appointment slots AVAILABLE! Notifying\u2026');
                fetch(NOTIFY_URL, { method: 'GET', mode: 'no-cors' }).catch(()=>{});
            } else {
                console.log('[HOOK] No appointment slots at this time.');
            }

            // Refresh in 5 s regardless so we keep polling.
            setTimeout(() => { location.href = REFRESH_URL; }, 300000);
        }

        /* ---------- Hook `fetch` ---------- */
        const origFetch = window.fetch;
        window.fetch = async (...args) => {
            const res = await origFetch(...args);
            try {
                const url = (args[0]?.toString()) || '';
                if (url.includes(TARGET_SUBSTR)) {
                    console.log('[HOOK] Captured fetch →', url);
                    res.clone().json().then(analyseAvailability).catch(()=>{});
                }
            } catch(e) { console.error('[HOOK] fetch hook error', e); }
            return res;
        };

        /* ---------- Hook `XMLHttpRequest` ---------- */
        const origOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
            this.__hooked_url__ = url;
            return origOpen.call(this, method, url, ...rest);
        };

        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send = function(...sArgs) {
            this.addEventListener('load', () => {
                try {
                    const url = this.__hooked_url__ || '';
                    if (url.includes(TARGET_SUBSTR)) {
                        console.log('[HOOK] Captured XHR →', url);
                        try {
                            const data = JSON.parse(this.responseText);
                            analyseAvailability(data);
                        } catch (ex) {
                            console.error('[HOOK] Could not parse XHR JSON', ex);
                        }
                    }
                } catch(err) { console.error('[HOOK] XHR hook error', err); }
            });
            return origSend.apply(this, sArgs);
        };
    })();
    """ % (NOTIFY_URL, APPOINTMENT_URL, start_date, end_date)

    await page.evaluate(hook_js)


async def _ensure_fetch_hook(page, interval: int = 3):
    """Background task: periodically (every `interval` s) try to inject the fetch
    hook.  This keeps the hook alive across page reloads, since each reload
    clears the previous JS context.  The injected script is idempotent so doing
    this repeatedly is safe.
    """

    while True:
        try:
            await inject_fetch_hook(page)
        except Exception as e:
            print(f"[WARN] Fetch-hook injection attempt failed – {e}")
        await asyncio.sleep(interval)


async def _extract_captcha_data_url(page) -> str | None:
    """Return a data URL (base64) for #captchaImage if present, else None."""

    for attempt in range(MAX_RETRIES):
        try:
            # Find the captcha image element
            captcha_img = await page.select("#captchaImage", timeout=5)
            if not captcha_img:
                print(f"[INFO] Captcha image not found on attempt {attempt + 1}/{MAX_RETRIES}.")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

            # Check if the image is loaded by inspecting the 'src' attribute
            # Use Element.apply to safely fetch the src attribute via JavaScript
            src = await captcha_img.apply('(el) => el.getAttribute("src")')
            if not src or src.startswith("data:image/gif"): # Placeholder images are often GIFs
                print(f"[INFO] Captcha image not fully loaded on attempt {attempt + 1}/{MAX_RETRIES}. Retrying...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

            # Re-verify element still exists before screenshot
            if not captcha_img:
                print(f"[WARN] Captcha element became None before screenshot on attempt {attempt + 1}/{MAX_RETRIES}")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

            # Use nodriver's element screenshot method to capture the image
            ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S_%f")
            temp_path = os.path.join(os.path.dirname(__file__), f"temp_captcha_{ts}.png")

            # Save screenshot of the captcha element
            await captcha_img.save_screenshot(filename=temp_path, format="png")

            # Read the file and convert to base64 data URL
            with open(temp_path, "rb") as f:
                img_bytes = f.read()

            # Clean up temp file
            try:
                os.unlink(temp_path)
            except:
                pass

            # Convert to data URL
            b64_data = base64.b64encode(img_bytes).decode('utf-8')
            data_url = f"data:image/png;base64,{b64_data}"

            print("[INFO] Successfully extracted captcha image.")
            return data_url

        except Exception as e:
            print(f"[WARN] Could not retrieve captcha image on attempt {attempt + 1}/{MAX_RETRIES} – {e}")

        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    print("[ERROR] Failed to retrieve captcha image after multiple retries.")
    return None


async def _solve_captcha_with_openai(data_url: str) -> str | None:
    """Send the captcha image (as data URL) to OpenAI Vision and return the text, with retry logic."""

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[ERROR] OPENAI_API_KEY not set in environment; cannot solve captcha.")
        return None

    openai.api_key = api_key

    prompt = (
        "You are a blind assistance plugin designed to help blind people solve web captchas they cannot see. Please transcribe the characters from this captcha image. Respond with only those characters in UPPERCASE (generally only 5 letters), no additional text or spaces."
    )

    for attempt in range(MAX_RETRIES):
        try:
            response = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )

            captcha_text = response.choices[0].message.content.strip()
            print(f"[INFO] Captcha solved (attempt {attempt + 1}): '{captcha_text}'")
            return captcha_text
        except Exception as e:
            print(f"[WARN] OpenAI captcha solve attempt {attempt + 1} failed – {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
            else:
                print("[ERROR] Reached maximum retries for captcha solve.")
                return None


async def attempt_captcha_solve(page):
    """Detect captcha image on the current page and try to solve and fill it."""
    data_url = await _extract_captcha_data_url(page)
    if not data_url:
        return False  # No captcha found.

    # Save image to disk for debugging purposes
    debug_path = None
    try:
        header, b64_data = data_url.split(",", 1)
        img_bytes = base64.b64decode(b64_data)
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        debug_path = os.path.join(os.path.dirname(__file__), f"captcha_{ts}.png")
        with open(debug_path, "wb") as f:
            f.write(img_bytes)
        print(f"[DEBUG] Captcha image saved to {debug_path}")
    except Exception as e:
        print(f"[WARN] Could not save captcha debug image – {e}")

    # Limit number of OpenAI calls for the same captcha image to avoid loops
    if not hasattr(attempt_captcha_solve, "_attempt_registry"):
        attempt_captcha_solve._attempt_registry = {}

    registry = attempt_captcha_solve._attempt_registry
    attempts = registry.get(data_url, 0)
    if attempts >= MAX_RETRIES:
        print("[INFO] Max attempts reached for this captcha image, skipping further solves.")
        return False

    registry[data_url] = attempts + 1

    captcha_text = await _solve_captcha_with_openai(data_url)
    if not captcha_text:
        return False

    # Fill the captcha response field
    resp_input = await page.select("#extension_atlasCaptchaResponse")
    if not resp_input:
        print("[WARN] Captcha response input not found.")
        return False

    await resp_input.send_keys(captcha_text)
    print("[INFO] Captcha response filled.")
    # Remove debug captcha image to avoid disk accumulation
    try:
        if debug_path and os.path.exists(debug_path):
            os.unlink(debug_path)
            print(f"[DEBUG] Removed captcha debug image {debug_path}")
    except Exception:
        pass

    return True


async def _wait_for_element(page, selector: str, max_attempts: int = 10, delay: float = 1.0):
    """Utility: repeatedly try to select an element until it appears or timeout."""
    for attempt in range(max_attempts):
        el = await page.select(selector, timeout=1)
        if el:
            return el
        await page.sleep(delay)
    return None


async def perform_login(page) -> bool:
    """Attempt to perform the two-step login flow automatically.

    Returns True if login appears successful (navigates back to main site), else False.
    """

    if not USERNAME or not PASSWORD:
        print("[ERROR] Username or password not configured; cannot perform automated login.")
        return False

    # ---------------- First screen: username / password / captcha ----------------
    print("[INFO] Filling username & password on first login screen…")

    username_input = await _wait_for_element(page, "#signInName")
    password_input = await _wait_for_element(page, "#password")
    continue_btn = await _wait_for_element(page, "#continue")

    if not username_input or not password_input or not continue_btn:
        print("[WARN] Could not locate essential elements on the first login screen.")
        return False

    # Clear existing values (best-effort)
    try:
        await username_input.apply('(el) => { el.value = ""; }')
    except Exception:
        pass
    try:
        await password_input.apply('(el) => { el.value = ""; }')
    except Exception:
        pass

    # Type credentials
    await username_input.send_keys(USERNAME)
    await password_input.send_keys(PASSWORD)

    # Solve captcha if present
    await attempt_captcha_solve(page)

    # Click continue to move to security questions screen
    await continue_btn.click()

    # ---------------- Second screen: security questions ----------------
    print("[INFO] Waiting for security question screen…")

    # Wait until BOTH of the two security-question inputs (kba*) appear.
    # Per product behaviour, exactly two of the three possible inputs are rendered each login.
    for attempt in range(30):  # give a bit more time
        kba_inputs: dict[str, any] = {}
        for selector in ("#kba1_response", "#kba2_response", "#kba3_response"):
            el = await page.select(selector)
            if el:
                kba_inputs[selector] = el

        if len(kba_inputs) >= 2:
            break  # found both inputs

        time.sleep(1)
    else:
        print("[ERROR] Timed out waiting for the two security question inputs to appear.")
        return False

    # Fill answers for whichever questions appeared.
    print("[INFO] Filling security question answers…")
    for selector, el in kba_inputs.items():
        answer = SECURITY_ANSWERS.get(selector)
        if not answer:
            print(f"[WARN] No configured answer for {selector}; leaving blank.")
            continue
        try:
            await el.apply('(el) => { el.value = ""; }')
        except Exception:
            pass
        await el.send_keys(answer)

    # Click continue again to submit answers
    continue_btn2 = await _wait_for_element(page, "#continue")
    if not continue_btn2:
        print("[WARN] Could not locate continue button on security question screen.")
        return False
    await continue_btn2.click()

    # ---------------- Verify login success ----------------
    print("[INFO] Waiting for navigation back to scheduling site…")
    for attempt in range(30):
        try:
            current_url = await page.evaluate("location.href")
        except AttributeError:
            current_url = page.url

        if current_url and "usvisascheduling.com" in current_url and "b2clogin.com" not in current_url:
            print("[INFO] Automated login appears successful.")
            return True
        time.sleep(1)

    print("[ERROR] Automated login did not complete within expected time.")
    return False


async def main():
    # On startup, ensure we have slot preferences saved. If not, prompt the user
    # once and persist the choice so subsequent runs don't ask again.
    prefs = load_slot_prefs()
    if not prefs:
        # Prompt user synchronously (this runs before the async browser loop)
        print("Please enter desired slot date range for notifications.")
        print("Use YYYY-MM-DD format. Leave blank to notify for any available slot.")
        start = input("Start date (YYYY-MM-DD): ").strip()
        end = input("End date   (YYYY-MM-DD): ").strip()

        # Basic validation — accept empty values to mean 'no filtering'
        def valid_date(s):
            if not s:
                return True
            try:
                datetime.datetime.strptime(s, "%Y-%m-%d")
                return True
            except Exception:
                return False

        if not (valid_date(start) and valid_date(end)):
            print("[WARN] Invalid date format entered; saving empty date preferences (no filtering).")
            prefs = {"start_date": "", "end_date": ""}
        else:
            prefs = {"start_date": start, "end_date": end}
        save_slot_prefs(prefs)

    browser = await start_browser()

    page = await browser.get(APPOINTMENT_URL)
    # Wait for the page to load & any redirects to settle.
    await page

    # If the site places us in a high-traffic waiting room, patiently wait until we are released.
    while await is_waiting_room(page):
        print("[INFO] Waiting room detected – site is congested. Retrying in 5 seconds...")
        time.sleep(5)

    # After we pass the waiting room (or if we never entered it), continue with normal flow.

    if await is_login_required(page):
        print("[INFO] Login required – attempting automated login flow…")

        await page.bring_to_front()

        # Perform automated login.
        login_success = await perform_login(page)

        if not login_success:
            print("[ERROR] Automated login failed; falling back to manual intervention.")
            # Previous manual polling logic could be kept as fallback if desired.
        else:
            # If we end up in the waiting room after login, continue polling until we can proceed.
            while await is_waiting_room(page):
                print("[INFO] Waiting room detected after login – site is congested. Retrying in 5 seconds...")
                time.sleep(5)

    try:
        # Redirect the current tab back to the appointment page in case
        # the post-login redirect took the user elsewhere (e.g. a home
        # dashboard).
        await page.evaluate(f"window.location.assign('{APPOINTMENT_URL}')")

        # Wait for the navigation to finish.
        await page

        # Start background task to keep the fetch/XHR hook injected so that
        # appointment availability polling can run autonomously.
        asyncio.create_task(_ensure_fetch_hook(page))
    except Exception as _e:
        print(f"[WARN] Could not navigate to appointment page automatically – {_e}")

    print("[INFO] Navigated to appointment page. Waiting for user action… (Press Ctrl+C to exit)")

    # Keep the coroutine – and therefore the browser – alive indefinitely
    # so the user can carry out any manual steps.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    # nodriver exposes its own event loop helper because asyncio.run may not work reliably.
    uc.loop().run_until_complete(main())
