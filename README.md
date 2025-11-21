## Prerequisites

- Python 3.8+ (or a compatible Python 3.x)
- OpenAI API key
- Telegram bot token and chat ID (for notifications) — can be provided via `.env` or environment variables

## Installation

1. Clone the repository:

```bash
git clone https://github.com/goeksu/usvisascheduling-bot
cd usvisascheduling-bot
```

2. Create and activate a Python virtual environment (macOS / zsh):

```bash
# create venv named .venv
python3 -m venv .venv

# activate in zsh
source .venv/bin/activate

# install requirements
pip install -r requirements.txt
```

3. Add your OpenAI API key to a `.env` file (or environment):

```
OPENAI_API_KEY=your_openai_api_key_here
TELEGRAM_BOT_TOKEN="XXX"
TELEGRAM_CHAT_ID="XXX"
```

4. Add credentials to `credential.json` (example):

```json
{
	"username": "your_username",
	"password": "your_password",
	"security_questions": [ ... ]
}
```

## Usage

Run the bot inside the activated virtual environment:

```bash
python visa_checker.py
```
