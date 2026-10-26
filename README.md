# PatientProbe — Automated Voice Agent Testing

PatientProbe is a Python-based voice testing agent that simulates patients calling an AI-powered medical receptionist. It conducts multi-turn conversations, tests realistic patient workflows and edge cases, records interactions, and generates speaker-labeled transcripts for reviewing conversational quality and identifying potential issues.

The project uses **LiveKit Agents in pipeline mode**, with separate speech-to-text, language model, and text-to-speech components.

## Technology Stack

| Component | Technology |
|---|---|
| Agent framework | LiveKit Agents (Python) |
| Speech-to-text | Deepgram Nova-3 |
| Language model | OpenAI GPT-4o |
| Text-to-speech | Deepgram Aura-2 Thalia |
| Voice activity detection | Silero VAD |
| Telephony | LiveKit outbound SIP |
| Language | Python 3.12.8 |
| Recording | LiveKit session recording |
| Transcripts | Local TXT and JSONL files |

No real-time speech-to-speech models or hosted voice-agent platforms are used.

## How It Works

1. The user selects a patient scenario through `make_call.py`.
2. The launcher creates a LiveKit room and dispatches the PatientProbe worker with the selected scenario.
3. The worker starts its voice session before initiating the outbound SIP call.
4. PatientProbe interacts with the receptionist using the scenario's patient identity, objectives, and conversational instructions.
5. The agent processes incoming speech using Deepgram STT, generates responses with GPT-4o, and speaks using Deepgram TTS.
6. LiveKit captures the session audio while the application saves speaker-labeled transcripts and run metadata.
7. Recorded calls and transcripts are reviewed to identify behavioral issues, quality concerns, and edge cases.

The assessment destination is fixed in `agent.py` to prevent accidental calls to other numbers.

## Project Structure

```text
voiceprobe/
├── agent.py               # Voice agent, SIP calling, and transcript capture
├── make_call.py           # Scenario launcher
├── scenarios.py           # Patient scenarios and instructions
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── README.md              # Setup and usage
├── ARCHITECTURE.md        # Design decisions and tradeoffs
├── BUG_REPORT.md          # Findings with supporting evidence
├── call_log.csv           # Call execution log
└── recordings/
    └── scenario_XX/
        └── YYYYMMDD_HHMMSS/
            ├── call.mp3
            ├── transcript.txt
            ├── transcript.jsonl
            └── meta.json
```

Some run directories may not contain every artifact; see the available files for each run.

## Prerequisites

- Python 3.12 (developed using Python 3.12.8)
- LiveKit Cloud project
- Configured outbound SIP trunk and provisioned caller number
- Deepgram API key
- OpenAI API key
- Network access to the configured services

The outbound SIP trunk must be configured in LiveKit Cloud before making calls.

## Installation

Clone the repository and enter the project directory:

```bash
git clone https://github.com/AP2001211/patientprobe
cd voiceprobe
```

Create and activate a Python virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create the environment configuration:

```bash
cp .env.example .env
```

Fill in the required credentials and telephony settings:

| Variable | Purpose |
|---|---|
| `LIVEKIT_URL` | LiveKit Cloud server URL |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `SIP_OUTBOUND_TRUNK_ID` | Configured outbound SIP trunk |
| `CALLER_NUMBER` | Provisioned outbound phone number in E.164 format |
| `DEEPGRAM_API_KEY` | Deepgram STT and TTS authentication |
| `OPENAI_API_KEY` | GPT-4o authentication |

Optional settings:

| Variable | Default |
|---|---|
| `DIAL_TIMEOUT_SECONDS` | `30` |
| `MAX_CALL_SECONDS` | `360` |

Do not commit your `.env` file or API credentials.

## Running a Test Call

Start the LiveKit agent worker in one terminal:

```bash
python agent.py dev
```

In a second terminal, activate the same virtual environment and launch a scenario:

```bash
python make_call.py 13
```

The launcher creates a LiveKit room and dispatches the selected scenario. The worker handles the outbound call and conversation.

The destination number is fixed in the application for this assessment. Running a scenario places a real outbound call and may incur API and telephony charges.

### Scenarios with appointment dependencies

Some scenarios operate on appointments established in earlier calls. For example:

```bash
python make_call.py 02 --booked-slot "Tuesday, November 3 at 2:30 PM"
```

```bash
python make_call.py 03 --current-slot "Thursday, November 5 at 3:00 PM"
```

These values must reflect appointments actually confirmed during earlier conversations. The launcher requests confirmation before proceeding unless explicitly overridden.

See `scenarios.py` for the available scenario definitions.

## Recordings and Transcripts

The repository includes 20 recorded test calls across multiple scenarios and repeated runs.

Run artifacts are organized under:

```text
recordings/scenario_XX/YYYYMMDD_HHMMSS/
```

Where available, each run includes:

- `call.mp3` — audio recording
- `transcript.txt` — readable, speaker-labeled transcript
- `transcript.jsonl` — structured conversation turns
- `meta.json` — run metadata and call outcome information

**Recording workflow:** The agent enables LiveKit session recording during calls. Audio exports were downloaded from LiveKit Cloud, converted to MP3, and matched to local run directories for submission. Automatic downloading of audio exports is not part of the application.

All original test runs are preserved for transparency. Some runs include incomplete conversations or abrupt endings; these are retained as part of the testing history rather than represented as successful full conversations.

## Test Coverage

PatientProbe tests a range of patient-receptionist interactions, including:

- Appointment scheduling and follow-up visits
- Appointment rescheduling and cancellation
- Medication refill requests
- Office information and administrative questions
- Identity and demographic corrections
- Requests for human assistance
- Ambiguous or unusual requests
- Conversational interruptions and recovery behavior

The scenarios are designed to test both the receptionist's responses and the patient simulator's ability to maintain a realistic, coherent conversation.

## Findings

See [BUG_REPORT.md](BUG_REPORT.md) for documented observations, supporting transcripts, timestamps, severity assessments, and expected behavior.

Findings distinguish observed receptionist behavior from limitations in the test environment or the patient simulator itself.

## Design Decisions

See [ARCHITECTURE.md](ARCHITECTURE.md) for the reasoning behind the STT, LLM, TTS, VAD, telephony, turn-taking, and recording choices, including implementation tradeoffs and limitations.

## Limitations

- LiveKit Cloud is required for call execution and audio export.
- Calls incur provider and telephony usage costs.
- Audio exports are collected after calls rather than downloaded automatically by the Python application.
- Voice interaction quality depends on network conditions, provider latency, endpointing, and turn-taking behavior.
- Some test conversations end prematurely or include incomplete transfers; these are retained as evidence of actual test execution.

## Assessment Scope

PatientProbe is an automated testing prototype, not a production medical assistant. It uses simulated patient scenarios to evaluate an external voice receptionist and does not provide medical advice or independently manage real patient records.
