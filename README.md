# Bedrock Chat

A Flask chat app for Claude on Amazon Bedrock (boto3 Converse API, streaming).
Conversations are stored in SQLite.

| Path | What it is |
|---|---|
| `app.py` | Flask app: pages, conversation API, Bedrock streaming |
| `database.py` | SQLite storage for conversations and messages |
| `templates/`, `static/` | Chat UI |
| `Dockerfile`, `compose.yaml`, `compose.local.yaml` | Container image and run configs |
| `deploy/bedrock-policy.json` | IAM policy the app needs on EC2 |
| `test_bedrock.py` | Standalone Bedrock connectivity check |

## How AWS credentials work

The app never reads keys itself. boto3 uses its default credential chain, so the
same image works everywhere:

- **Locally**: your AWS CLI configuration (`~/.aws/credentials`), mounted into the
  container **read-only at runtime**. The keys are never copied into the image
  (`.dockerignore` also excludes `.aws/` and `.env`).
- **On EC2**: the instance's IAM role. No keys are stored on the server.

## Run locally with Python

```powershell
.venv\Scripts\python app.py        # http://127.0.0.1:5000
```

Conversations are saved to `instance/chat.db`. Set `DATABASE_PATH` to keep the
database outside OneDrive, which can lock or corrupt SQLite files while syncing.

## Run locally with Docker

```powershell
docker compose -f compose.yaml -f compose.local.yaml up --build -d   # http://127.0.0.1:8001
docker compose -f compose.yaml -f compose.local.yaml logs -f          # follow logs
docker compose -f compose.yaml -f compose.local.yaml down             # stop (keeps data)
```

- Uses the `default` profile. For another one, set `$env:AWS_PROFILE = "name"` first.
- Conversations live in the Docker volume `bedrock-chat_chat-data` and survive
  rebuilds. `down -v` deletes them.
- Change the host port with `$env:HOST_PORT = "9000"`.

## Deploy to EC2

### 1. Create an IAM role for the instance

Create a role for the **EC2** service and attach:

- an inline policy from `deploy/bedrock-policy.json` (lets the role call Claude Opus 4.6 on Bedrock),
- `AmazonSSMManagedInstanceCore` (for the private access option in step 5),
- `AmazonEC2ContainerRegistryReadOnly` if you pull the image from ECR.

Attach the role to the instance (**Actions > Security > Modify IAM role**).
Don't copy access keys to the server.

### 2. Allow containers to reach the instance role

Containers add a network hop, and IMDSv2 rejects credential requests that
travel more than one hop by default on many AMIs. Set the hop limit to 2:

```bash
aws ec2 modify-instance-metadata-options --instance-id i-0123456789abcdef0 \
  --http-endpoint enabled --http-tokens required --http-put-response-hop-limit 2
```

### 3. Install Docker (Amazon Linux 2023)

```bash
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user   # log out and back in afterwards
```

### 4. Get the image onto the instance

**Option A: build on the instance.** Copy the project (git or `scp`), then:

```bash
docker build -t bedrock-chat:latest .
```

**Option B: push to Amazon ECR.** Build for the instance's CPU: `linux/amd64`
for Intel/AMD types, `linux/arm64` for Graviton (`t4g`, `m7g`, ...).

```bash
ACCOUNT=123456789012; REGION=us-east-1; REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/bedrock-chat
aws ecr create-repository --repository-name bedrock-chat --region $REGION
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker buildx build --platform linux/amd64 -t $REPO:latest --push .
# On the instance: run the same login command, then
docker pull $REPO:latest && docker tag $REPO:latest bedrock-chat:latest
```

### 5. Run it

```bash
docker run -d --name bedrock-chat --restart unless-stopped \
  -p 127.0.0.1:8001:8000 \
  -e AWS_REGION=us-east-1 \
  -v bedrock-chat-data:/data \
  bedrock-chat:latest
```

Confirm the container is using the instance role:

```bash
docker exec bedrock-chat python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
# expected: arn:aws:sts::<account>:assumed-role/<role-name>/i-...
```

### 6. Access it safely

The app has email and password accounts (see [Accounts](#accounts)), but **anyone who
can reach it can create an account and run Bedrock calls billed to your AWS account**
unless you set `ALLOW_SIGNUP=0` after creating your own. Don't open its port to
`0.0.0.0/0`. Login passwords travel unencrypted over plain HTTP.

- **Private (recommended):** keep the `127.0.0.1` binding above and forward the
  port over SSM. No inbound security group rules are needed. Requires the
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  on your computer:

  ```bash
  aws ssm start-session --target i-0123456789abcdef0 \
    --document-name AWS-StartPortForwardingSession \
    --parameters "portNumber=8001,localPortNumber=8001"
  ```

  Then open http://localhost:8001.

- **Direct:** run with `-p 8001:8000` and allow port 8001 in the security group
  **only from your IP**. Traffic is unencrypted HTTP. For anything broader, put it
  behind an HTTPS load balancer and add `-e COOKIE_SECURE=1` so login cookies are
  only sent over HTTPS.

### Backups

The database is a single file in the `bedrock-chat-data` volume:

```bash
docker exec bedrock-chat python -c "import sqlite3; s=sqlite3.connect('/data/chat.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
docker cp bedrock-chat:/data/backup.db ./chat-backup.db
```

## Accounts

Visitors sign up and sign in with an email and password (at least 8 characters).
Each person sees only their own conversations.

- Passwords are stored as salted hashes, never as text. Five wrong passwords for
  one email lock sign-in for that email for 15 minutes.
- The login is a signed cookie (`HttpOnly`, `SameSite=Lax`) that lasts 30 days.
  Its key is generated on first start and saved as `secret_key` beside the
  database (in the `/data` volume in Docker). **Keep it private and back it up
  with the database.** Deleting it signs everyone out. Set `SECRET_KEY` to use
  your own.
- **Signing out clears the browser's cookie but can't cancel a copy of it** that
  was taken earlier; it stays valid until it expires or `secret_key` changes.
- The first account created takes over any conversations saved before accounts
  existed.
- There is no password reset or email verification yet. To change a password
  or remove an account, edit the `users` table in the database.
- To stop strangers from creating accounts, set `ALLOW_SIGNUP=0` after you have
  created yours.

## Automatic deployment (GitHub Actions)

Every push to `main` runs `.github/workflows/deploy.yml`:

1. Checks the Python compiles, builds the image (linux/amd64) and **smoke-tests it**
   (health check, sign-in redirect, sign-in page, protected API returns 401). A broken
   image stops here and nothing is deployed.
2. Pushes the image to ECR as `<commit SHA>` and `latest`.
3. Deploys it to the EC2 instance through AWS Systems Manager, running
   `deploy/deploy.sh` on the instance: pull the image, replace the container, wait for
   `/healthz`. **If the new version isn't healthy, the previous one is put back** and the
   workflow fails. The data volume (accounts, conversations) is never touched.

GitHub signs in to AWS with OIDC, so **no AWS keys are stored in GitHub**, and nothing
connects to the instance over SSH.

### One-time setup

1. Run `bash deploy/setup-aws.sh` with admin AWS credentials. It attaches the Systems
   Manager permission to the instance's role, creates the GitHub sign-in trust if
   missing, and creates the role `devleap-alexk-github-deploy`. That role can only be
   assumed by **this repository's `main` branch**, and can only push to the one ECR
   repository and run a script on the one instance.
2. In GitHub, go to **Settings > Secrets and variables > Actions > Variables** and add
   two repository **variables** (not secrets); the script prints the values:
   - `AWS_DEPLOY_ROLE_ARN`
   - `EC2_INSTANCE_ID`
3. Push to `main`, or run the workflow from the **Actions** tab (**Run workflow**).

### Things to know

- Anyone who can push to `main` can run commands on the instance through this
  workflow. Protect the branch (require pull requests) if more people get access.
- The instance's IP address isn't used, so it can change without breaking deploys.
  The instance ID and ECR repository name are fixed in `setup-aws.sh` and the workflow.
- To go back to an earlier version, revert the commit and push, or re-run the workflow
  on an earlier commit from the Actions tab. Every image stays in ECR under its commit SHA.
- Deploys run one at a time. The container is replaced, so there are a few seconds of
  downtime per deploy.

## Conversation memory

Each request sends Claude only your most recent messages (with the replies between
them), not the whole chat. The default is **3**: your new message plus the two
before it. Input tokens rise over the first three messages and then stay level
instead of growing with every message.

- The full conversation stays saved and visible. Claude just can't see anything
  older, so it forgets details from earlier in a long chat.
- Each person can change it in **Chat settings > Conversation memory** (1 to 50).
  `CONTEXT_MESSAGES` sets the default for everyone.
- The level isn't exactly constant, because it depends on how long your messages
  and Claude's replies are. A very long reply stays in the window until it falls out.

## Plans

| Plan | Messages | Price |
|---|---|---|
| Free | 10 in total, for the life of the account | $0 |
| Pro | Unlimited | $10 a month |

- The limit is enforced on the server, per account. It counts messages **sent**, so
  deleting conversations doesn't give messages back. Retrying a failed or stopped
  reply is free. A message that gets no reply because of an AWS error isn't counted.
- When the limit is reached the message box shows a notice, and the server answers
  further messages with HTTP 402.
- **Payment isn't connected yet**: the Upgrade buttons do nothing (`startCheckout()`
  in `static/js/app.js` is where checkout will start). Until then, move an account
  to Pro by hand:

  ```powershell
  docker exec bedrock-chat-app-1 python -c "import database as db; db.init('/data/chat.db'); print(db.set_plan('you@example.com', 'pro'))"
  ```

  Use `'free'` instead of `'pro'` to move it back. When a payment provider is added,
  its "payment succeeded" and "subscription cancelled" callbacks should call
  `db.set_plan(...)` in the same way.
- Accounts that existed before plans were added start on Free with 0 messages used.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Region for Bedrock calls |
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-opus-4-6-v1` | Model or inference profile |
| `MAX_TOKENS` | `4096` | Default maximum answer length |
| `TEMPERATURE` | `0.5` | Default temperature |
| `SYSTEM_PROMPT` | built-in | Default system prompt |
| `DATABASE_PATH` | `instance/chat.db` (`/data/chat.db` in Docker) | SQLite file |
| `SECRET_KEY` | generated, saved beside the database | Signs login cookies |
| `COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS |
| `ALLOW_SIGNUP` | `1` | Set to `0` to close new signups |
| `FREE_MESSAGE_LIMIT` | `10` | Messages a free account can send in total |
| `CONTEXT_MESSAGES` | `3` | Recent messages sent to Claude with each request |
| `AWS_PROFILE` | `default` | Profile from `~/.aws` (local only) |
| `HOST_PORT`, `BIND_ADDRESS` | `8001`, `127.0.0.1` | Compose port mapping |

The container runs gunicorn with **one worker process and 16 threads**. Keep it
at one process: in-progress replies are tracked in memory so Stop can reach them.
