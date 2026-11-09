"""Bedrock Chat: a Flask chat bot backed by Claude on Amazon Bedrock.

Conversations are stored in SQLite. For each new message the server loads the
conversation history from the database, sends it to the Bedrock Converse API,
streams the reply back to the browser as newline-delimited JSON (NDJSON), and
saves the reply when the stream ends.

Run:
    python app.py            # development server on http://127.0.0.1:5000
    docker compose -f compose.yaml -f compose.local.yaml up --build -d
                             # Docker (gunicorn) on http://127.0.0.1:8001; see README.md

Configuration (environment variables, all optional):
    AWS_REGION         AWS region for Bedrock         (default: us-east-1)
    BEDROCK_MODEL_ID   Bedrock model / profile ID     (default: global.anthropic.claude-opus-4-6-v1)
    MAX_TOKENS         Default max response tokens    (default: 4096)
    TEMPERATURE        Default sampling temperature   (default: 0.5)
    SYSTEM_PROMPT      Default system prompt
    DATABASE_PATH      SQLite database file           (default: instance/chat.db)
    SECRET_KEY         Signs login cookies            (default: generated, saved next to the database)
    COOKIE_SECURE      Set to 1 when served over HTTPS (default: 0)
    ALLOW_SIGNUP       Set to 0 to close new signups  (default: 1)
    FREE_MESSAGE_LIMIT Messages a free account can send in total (default: 10)
    CONTEXT_MESSAGES   Recent messages Claude sees (default: 3)
    HOST / PORT        Where the server listens       (default: 127.0.0.1:5000)
"""

import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import timedelta
from functools import wraps

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from flask import (
    Flask, Response, g, jsonify, redirect, render_template, request, session, stream_with_context,
)
from werkzeug.security import check_password_hash, generate_password_hash

import database as db

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")

DEFAULT_MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
MAX_TOKENS_LIMIT = 32000
DEFAULT_TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.5"))
DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful, knowledgeable assistant. Answer clearly and concisely, "
    "and use Markdown (headings, lists, tables, fenced code blocks) when it "
    "makes the answer easier to read.",
)
DEFAULT_CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "3"))
MAX_CONTEXT_MESSAGES = 50
MAX_SYSTEM_PROMPT_CHARS = 20_000
MAX_TITLE_CHARS = 120
DEFAULT_TITLE = "New chat"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bedrock-chat")

app = Flask(__name__)
# Upper bound on a single request body (one message plus settings).
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "chat.db"))
db.init(DATABASE_PATH)


def load_secret_key(directory: str) -> str:
    """Use SECRET_KEY if set; otherwise create one and keep it beside the database.

    Keeping it on disk means people stay signed in across restarts, and the
    key lives on the same volume as the data (so it isn't baked into the image).
    """
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    path = os.path.join(directory, "secret_key")
    for _ in range(2):
        try:
            with open(path, encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue  # another process just created it: read it
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    raise RuntimeError(f"Couldn't create a secret key at {path}. Set the SECRET_KEY variable instead.")


# --------------------------------------------------------------------------- #
# Authentication settings
# --------------------------------------------------------------------------- #

FREE_MESSAGE_LIMIT = int(os.environ.get("FREE_MESSAGE_LIMIT", "10"))  # lifetime total per free account
PRO_PRICE_LABEL = "$10 a month"
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "1") != "0"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_EMAIL_LENGTH = 254
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LOGIN_FAILURE_LIMIT = 5          # failed attempts allowed per email...
LOGIN_FAILURE_WINDOW = 15 * 60   # ...within this many seconds

app.config.update(
    SECRET_KEY=load_secret_key(os.path.dirname(os.path.abspath(DATABASE_PATH))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # the browser won't send the cookie on cross-site POSTs
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Compared against when the email is unknown, so timing doesn't reveal which emails exist.
_DUMMY_HASH = generate_password_hash("not-a-real-password")

_failures_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}

# boto3 clients are thread-safe, so create them once and share them.
aws_session = boto3.Session(region_name=AWS_REGION)
bedrock = aws_session.client(
    "bedrock-runtime",
    config=Config(
        connect_timeout=10,
        read_timeout=300,  # long answers can take a while to stream
        retries={"max_attempts": 3, "mode": "standard"},
    ),
)
sts = aws_session.client("sts")


# --------------------------------------------------------------------------- #
# Active streams: one reply at a time per conversation, and a way to stop it
# --------------------------------------------------------------------------- #

class ActiveStream:
    def __init__(self):
        self.cancel = threading.Event()    # set by the Stop endpoint
        self.finished = threading.Event()  # set once the reply is saved

_streams_lock = threading.Lock()
_active_streams: dict[str, ActiveStream] = {}


def claim_stream(conversation_id: str) -> ActiveStream | None:
    """Reserve the conversation for a new reply, or return None if one is running."""
    with _streams_lock:
        current = _active_streams.get(conversation_id)
        if current is None:
            claimed = ActiveStream()
            _active_streams[conversation_id] = claimed
            return claimed
    # A reply that was just stopped may still be saving; give it a moment.
    if current.cancel.is_set() and current.finished.wait(timeout=10):
        return claim_stream(conversation_id)
    return None


def release_stream(conversation_id: str, stream: ActiveStream) -> None:
    with _streams_lock:
        if _active_streams.get(conversation_id) is stream:
            del _active_streams[conversation_id]
    stream.finished.set()


def cancel_stream(conversation_id: str) -> None:
    with _streams_lock:
        current = _active_streams.get(conversation_id)
    if current:
        current.cancel.set()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def model_display_name(model_id: str) -> str:
    """Turn 'global.anthropic.claude-opus-4-6-v1' into 'Claude Opus 4.6'."""
    match = re.search(r"claude-(opus|sonnet|haiku|fable)-(\d{1,2})(?:-(\d{1,2}))?(?!\d)", model_id)
    if not match:
        return model_id
    family, major, minor = match.groups()
    version = f"{major}.{minor}" if minor else major
    return f"Claude {family.title()} {version}"


def friendly_aws_error(error: Exception) -> tuple[str, int]:
    """Map boto errors to a message the user can act on, plus an HTTP status."""
    if isinstance(error, NoCredentialsError):
        return ("AWS credentials not found. Locally, run `aws configure` (or mount ~/.aws into "
                "the container); on EC2, attach an IAM role to the instance. Then restart the app."), 503

    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "Error")
        message = error.response.get("Error", {}).get("Message", str(error))
        hints = {
            "AccessDeniedException": (
                f"This AWS identity can't invoke {MODEL_ID}. Check that model access is "
                "enabled in the Bedrock console and that your IAM policy allows "
                "bedrock:InvokeModelWithResponseStream.",
                403,
            ),
            "ThrottlingException": ("Bedrock is throttling requests. Wait a few seconds and retry.", 429),
            "ServiceUnavailableException": ("Bedrock is temporarily unavailable. Retry in a moment.", 503),
            "ModelNotReadyException": ("The model isn't ready yet. Retry in a moment.", 503),
            "ExpiredTokenException": ("Your AWS session token has expired. Refresh your credentials and retry.", 401),
            "UnrecognizedClientException": ("AWS rejected the credentials. Check your access key and retry.", 401),
            "ResourceNotFoundException": (f"Model {MODEL_ID} wasn't found in {AWS_REGION}. Check BEDROCK_MODEL_ID.", 404),
        }
        if code in hints:
            return hints[code]
        return f"{code}: {message}", 502

    return f"Couldn't reach Amazon Bedrock: {error}", 502


def build_converse_messages(rows: list[dict], context_messages: int) -> list[dict]:
    """Convert stored messages to Converse API format.

    Failed replies and empty messages are skipped, and consecutive messages
    from the same role are merged, because Converse requires strictly
    alternating roles that start and end with the user.

    Only the last `context_messages` user messages (with the replies between
    them) are sent. That keeps the input size, and so the token cost of each
    request, roughly constant however long the conversation gets. The full
    history stays saved; Claude just doesn't see the older part.
    """
    merged: list[dict] = []
    for row in rows:
        if row["role"] == "assistant" and row["status"] == "error":
            continue
        text = (row["content"] or "").strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == row["role"]:
            merged[-1]["text"] += "\n\n" + text
        else:
            merged.append({"role": row["role"], "text": text})

    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    if not merged or merged[-1]["role"] != "user":
        raise ValueError("There's no message to reply to. Send a new message instead.")

    user_positions = [i for i, m in enumerate(merged) if m["role"] == "user"]
    if len(user_positions) > context_messages:
        merged = merged[user_positions[-context_messages]:]

    return [{"role": m["role"], "content": [{"text": m["text"]}]} for m in merged]


def read_settings(payload: dict) -> tuple[int, float, str, int]:
    """Read per-request settings from the browser, clamped to safe ranges."""
    settings = payload.get("settings") or {}

    try:
        max_tokens = int(settings.get("maxTokens", DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    max_tokens = max(1, min(max_tokens, MAX_TOKENS_LIMIT))

    try:
        temperature = float(settings.get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        temperature = DEFAULT_TEMPERATURE
    temperature = max(0.0, min(temperature, 1.0))

    system_prompt = settings.get("system", DEFAULT_SYSTEM_PROMPT)
    if not isinstance(system_prompt, str):
        system_prompt = DEFAULT_SYSTEM_PROMPT
    system_prompt = system_prompt.strip()[:MAX_SYSTEM_PROMPT_CHARS]

    try:
        context_messages = int(settings.get("contextMessages", DEFAULT_CONTEXT_MESSAGES))
    except (TypeError, ValueError):
        context_messages = DEFAULT_CONTEXT_MESSAGES
    context_messages = max(1, min(context_messages, MAX_CONTEXT_MESSAGES))

    return max_tokens, temperature, system_prompt, context_messages


def account_summary(user: dict) -> dict:
    """The plan and message allowance shown in the sidebar."""
    pro = user["plan"] == "pro"
    used = user["messages_used"]
    return {
        "plan": user["plan"],
        "messagesUsed": used,
        "messageLimit": None if pro else FREE_MESSAGE_LIMIT,
        "messagesLeft": None if pro else max(FREE_MESSAGE_LIMIT - used, 0),
        "price": PRO_PRICE_LABEL,
    }


def clean_title(value) -> str:
    if not isinstance(value, str):
        return DEFAULT_TITLE
    title = " ".join(value.split())[:MAX_TITLE_CHARS]
    return title or DEFAULT_TITLE


def ndjson(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def not_found():
    return jsonify(error="That conversation doesn't exist. It may have been deleted."), 404


# --------------------------------------------------------------------------- #
# Streaming a reply
# --------------------------------------------------------------------------- #

def stream_reply(conversation_id: str, payload: dict, prepare, charge_user_id: int | None = None):
    """Generate the next assistant reply for a conversation and stream it.

    `prepare` runs after the conversation is reserved and before the history
    is loaded (it saves the new user message, or removes a failed reply).

    If `charge_user_id` is given, one message is counted against that account
    first, and the request is refused (402) when its plan has no messages left.
    The message is given back if no reply is produced.
    """
    active = claim_stream(conversation_id)
    if active is None:
        return jsonify(error="A reply is already being written in this conversation. "
                             "Wait for it to finish or stop it first."), 409

    charged = charge_user_id is not None
    if charged and not db.consume_message(charge_user_id, FREE_MESSAGE_LIMIT):
        release_stream(conversation_id, active)
        return jsonify(
            error=f"You've used all {FREE_MESSAGE_LIMIT} free messages. "
                  "Upgrade to Pro to keep chatting.",
            code="limit_reached",
            account=account_summary(db.get_user(charge_user_id)),
        ), 402

    def refund():
        if charged:
            db.refund_message(charge_user_id)

    max_tokens, temperature, system_prompt, context_messages = read_settings(payload)

    # Start the Bedrock call before streaming so setup errors (credentials,
    # access, validation) come back as a normal HTTP error response.
    try:
        prepare()
        converse_args = {
            "modelId": MODEL_ID,
            "messages": build_converse_messages(db.history(conversation_id), context_messages),
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system_prompt:
            converse_args["system"] = [{"text": system_prompt}]
        response = bedrock.converse_stream(**converse_args)
    except ValueError as error:
        refund()
        release_stream(conversation_id, active)
        return jsonify(error=str(error)), 400
    except (ClientError, BotoCoreError) as error:
        message, status_code = friendly_aws_error(error)
        log.warning("converse_stream failed: %s", error)
        # Save the failure so it shows (with Retry) after a reload.
        db.add_message(conversation_id, "assistant", "", status="error", error=message)
        refund()  # no reply was produced, so it doesn't count
        release_stream(conversation_id, active)
        return jsonify(error=message), status_code
    except Exception:
        refund()
        release_stream(conversation_id, active)
        raise

    stream = response["stream"]

    def generate():
        parts: list[str] = []
        stop_reason = None
        usage = None
        error = None
        status = "stopped"  # kept if the user stops or the browser disconnects
        try:
            for event in stream:
                if active.cancel.is_set():
                    break
                if "contentBlockDelta" in event:
                    text = event["contentBlockDelta"].get("delta", {}).get("text")
                    if text:
                        parts.append(text)
                        yield ndjson({"type": "delta", "text": text})
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                    yield ndjson({"type": "stop", "stopReason": stop_reason})
                elif "metadata" in event:
                    raw = event["metadata"].get("usage", {})
                    usage = {
                        "inputTokens": raw.get("inputTokens"),
                        "outputTokens": raw.get("outputTokens"),
                        "latencyMs": event["metadata"].get("metrics", {}).get("latencyMs"),
                    }
                    yield ndjson({"type": "usage", **usage})
            else:
                if parts:
                    status = "done"
                else:
                    status, error = "error", "The model returned an empty response. Retry the message."
        except (ClientError, BotoCoreError) as exc:
            # Errors raised mid-stream (throttling, model stream errors, ...).
            error, _ = friendly_aws_error(exc)
            status = "error"
            log.warning("Stream interrupted: %s", exc)
            yield ndjson({"type": "error", "message": error})
        finally:
            # Runs on completion, on Stop, and when the browser disconnects.
            stream.close()
            db.add_message(
                conversation_id, "assistant", "".join(parts),
                status=status, error=error, stop_reason=stop_reason, usage=usage,
            )
            if status == "error" and not parts:
                refund()  # the stream failed before any text arrived

    result = Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    # Called after the generator is closed, even if it never started.
    result.call_on_close(lambda: release_stream(conversation_id, active))
    return result


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

@app.before_request
def load_user():
    """Look up the signed-in user from the session cookie."""
    g.user = None
    if request.endpoint in ("static", "healthz"):
        return
    user_id = session.get("uid")
    if user_id is not None:
        g.user = db.get_user(user_id)
        if g.user is None:  # the account no longer exists
            session.clear()


@app.after_request
def no_store_pages(response):
    # Don't let the browser show a signed-in page from its cache after sign-out.
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    return response


def login_required(view):
    """Pages: send signed-out visitors to the sign-in page."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapped


def api_login_required(view):
    """API: answer signed-out requests with 401 so the page can redirect."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return jsonify(error="Sign in to continue."), 401
        return view(*args, **kwargs)
    return wrapped


def sign_in(user: dict) -> None:
    session.clear()  # drop any old session state before starting a new one
    session["uid"] = user["id"]
    session.permanent = True


def login_locked(email: str) -> bool:
    cutoff = time.time() - LOGIN_FAILURE_WINDOW
    with _failures_lock:
        recent = [t for t in _login_failures.get(email, []) if t > cutoff]
        if recent:
            _login_failures[email] = recent
        else:
            _login_failures.pop(email, None)
        return len(recent) >= LOGIN_FAILURE_LIMIT


def record_login_failure(email: str) -> None:
    with _failures_lock:
        _login_failures.setdefault(email, []).append(time.time())


def clear_login_failures(email: str) -> None:
    with _failures_lock:
        _login_failures.pop(email, None)


def render_auth(mode: str, status_code: int = 200, error: str = "", email: str = ""):
    page = render_template(
        "auth.html",
        mode=mode,
        error=error,
        email=email,
        allow_signup=ALLOW_SIGNUP,
        min_password=MIN_PASSWORD_LENGTH,
        max_password=MAX_PASSWORD_LENGTH,
    )
    return page, status_code


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect("/")
    if request.method == "GET":
        return render_auth("login")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if login_locked(email):
        minutes = LOGIN_FAILURE_WINDOW // 60
        return render_auth(
            "login", 429, email=email,
            error=f"Too many failed attempts for this email. Try again in {minutes} minutes.",
        )

    user = db.get_user_by_email(email) if email else None
    # Always check a hash, so a wrong email takes as long as a wrong password.
    password_ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    if not (user and password_ok):
        record_login_failure(email)
        return render_auth("login", 401, email=email, error="The email or password is incorrect.")

    clear_login_failures(email)
    sign_in(user)
    return redirect("/")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect("/")
    if not ALLOW_SIGNUP:
        return render_auth("login", 403, error="New accounts are turned off. Ask the owner for an account.")
    if request.method == "GET":
        return render_auth("signup")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    error = ""
    if not EMAIL_PATTERN.match(email) or len(email) > MAX_EMAIL_LENGTH:
        error = "Enter a valid email address."
    elif len(password) < MIN_PASSWORD_LENGTH:
        error = f"Use a password with at least {MIN_PASSWORD_LENGTH} characters."
    elif len(password) > MAX_PASSWORD_LENGTH:
        error = f"Use a password with at most {MAX_PASSWORD_LENGTH} characters."
    elif password != confirm:
        error = "The two passwords don't match."
    if error:
        return render_auth("signup", 400, email=email, error=error)

    user = db.create_user(email, generate_password_hash(password))
    if user is None:
        return render_auth("signup", 409, email=email,
                           error="An account with this email already exists. Sign in instead.")
    sign_in(user)
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --------------------------------------------------------------------------- #
# Routes: pages
# --------------------------------------------------------------------------- #

def render_app(conversation_id: str | None = None):
    return render_template(
        "index.html",
        model_id=MODEL_ID,
        model_name=model_display_name(MODEL_ID),
        region=AWS_REGION,
        user_email=g.user["email"],
        account=account_summary(g.user),
        initial_conversation_id=conversation_id,
        defaults={
            "system": DEFAULT_SYSTEM_PROMPT,
            "temperature": DEFAULT_TEMPERATURE,
            "maxTokens": DEFAULT_MAX_TOKENS,
            "maxTokensLimit": MAX_TOKENS_LIMIT,
            "contextMessages": DEFAULT_CONTEXT_MESSAGES,
            "contextMessagesLimit": MAX_CONTEXT_MESSAGES,
        },
    )


@app.get("/")
@login_required
def index():
    return render_app()


@app.get("/c/<conversation_id>")
@login_required
def conversation_page(conversation_id):
    return render_app(conversation_id)


# --------------------------------------------------------------------------- #
# Routes: API
# --------------------------------------------------------------------------- #

@app.get("/healthz")
def healthz():
    """Liveness check for Docker and load balancers. Makes no AWS calls."""
    db.ping()
    return jsonify(ok=True)


@app.get("/api/status")
@api_login_required
def status():
    """Report which AWS identity the app is using (same check as test_bedrock.py)."""
    base = {"model": MODEL_ID, "modelName": model_display_name(MODEL_ID), "region": AWS_REGION}
    try:
        identity = sts.get_caller_identity()
        return jsonify(connected=True, account=identity["Account"], arn=identity["Arn"], **base)
    except (ClientError, BotoCoreError) as error:
        message, _ = friendly_aws_error(error)
        log.warning("AWS identity check failed: %s", error)
        return jsonify(connected=False, error=message, **base)


@app.get("/api/account")
@api_login_required
def account():
    return jsonify(account_summary(g.user))


@app.get("/api/conversations")
@api_login_required
def list_conversations():
    return jsonify(conversations=db.list_conversations(g.user["id"]))


@app.post("/api/conversations")
@api_login_required
def create_conversation():
    payload = request.get_json(silent=True) or {}
    return jsonify(db.create_conversation(g.user["id"], clean_title(payload.get("title")))), 201


@app.get("/api/conversations/<conversation_id>")
@api_login_required
def get_conversation(conversation_id):
    conversation = db.get_conversation(g.user["id"], conversation_id)
    return jsonify(conversation) if conversation else not_found()


@app.delete("/api/conversations/<conversation_id>")
@api_login_required
def delete_conversation(conversation_id):
    if not db.conversation_exists(g.user["id"], conversation_id):
        return not_found()
    cancel_stream(conversation_id)
    db.delete_conversation(g.user["id"], conversation_id)
    return "", 204


@app.post("/api/conversations/<conversation_id>/messages")
@api_login_required
def send_message(conversation_id):
    """Save the user's message and stream the assistant's reply."""
    payload = request.get_json(silent=True) or {}
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify(error="Type a message before sending."), 400
    if not db.conversation_exists(g.user["id"], conversation_id):
        return not_found()

    return stream_reply(
        conversation_id, payload,
        prepare=lambda: db.add_message(conversation_id, "user", content.strip()),
        charge_user_id=g.user["id"],
    )


@app.post("/api/conversations/<conversation_id>/retry")
@api_login_required
def retry_reply(conversation_id):
    """Replace the last (failed or stopped) reply with a new one."""
    payload = request.get_json(silent=True) or {}
    if not db.conversation_exists(g.user["id"], conversation_id):
        return not_found()

    return stream_reply(
        conversation_id, payload,
        prepare=lambda: db.delete_trailing_assistant(conversation_id),
    )


@app.post("/api/conversations/<conversation_id>/stop")
@api_login_required
def stop_reply(conversation_id):
    """Stop the reply being written and return it as saved.

    The reply may have finished just before the stop arrived, so the browser
    uses the saved message (status 'done' or 'stopped') as the final version.
    """
    if not db.conversation_exists(g.user["id"], conversation_id):
        return not_found()
    with _streams_lock:
        current = _active_streams.get(conversation_id)
    if current is None:
        return "", 204
    current.cancel.set()
    current.finished.wait(timeout=10)
    return jsonify(message=db.last_message(conversation_id))


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="This message is too long to send. Shorten it and try again."), 413


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    log.info("Bedrock Chat using %s in %s on http://%s:%s", MODEL_ID, AWS_REGION, host, port)
    log.info("Conversations are stored in %s", os.path.abspath(DATABASE_PATH))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
