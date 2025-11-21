#  US Visa Scheduling Bot

  
An automated bot for monitoring US visa appointment availability and handling the login process for the US visa scheduling system.

Please note that this bot is only applicable for regions using CGI platform for visa appointments.


##  Features

  

-  **Automated Login**: Handles the two-step login process including username/password authentication and security questions

-  **Captcha Solving**: Uses GPT-4o-mini to automatically solve captchas

-  **Appointment Monitoring**: Continuously monitors for available appointment slots

-  **Slot Filtering**: Filter the slots for desired dates

-  **Telegram Notifications**: Sends notifications when appointments become available

-  **Session Persistence**: Maintains login sessions across runs using browser profiles

-  **Waiting Room Handling**: Automatically handles high-traffic waiting rooms

  

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
