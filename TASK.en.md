# Cooking assistant: real-time voice + vision on Meta Ray-Ban glasses

Status: task specification (no code written yet). Chinese version: [TASK.zh.md](TASK.zh.md). The two files say the same thing.

## 1. Goal

The user cooks while wearing Meta Ray-Ban glasses. The assistant (Gemini) uses the glasses' microphone, speaker, camera and screen to do two things:

1. Before cooking: look at what is in front of the user, search for a recipe, and show it for confirmation.
2. While cooking: when the user calls Gemini, pull the current camera view and help by voice or by a visual highlight on an ingredient, pan or bowl.

## 2. User flow

### 2.1 Choosing a recipe

| Step | User | Assistant |
|---|---|---|
| 1 | Says "I'm going to cook this dish now, take a look" | Grabs the current frame and works out the dish (asks if it cannot tell) |
| 2 | | Searches the web for recipes, shows the first candidate on screen and asks "Is this recipe OK?" |
| 3a | Says "no" | Shows the next candidate and repeats step 2 (never re-shows a rejected recipe) |
| 3b | Says "yes" | Reads the full recipe page into the model context, then closes the recipe display |

### 2.2 Help while cooking

The user calls Gemini and asks a question. The assistant first pulls the current frame, then answers from the recipe plus what it sees. Two examples that must work:

- User: "What do I do next?"
  Assistant (voice): "The meat isn't cooked yet. Give it another minute or two, then add a spoonful of soy sauce."
  Requirement: the answer depends on both the current recipe step and the actual state in the frame. Reading the recipe aloud is not enough.
- User: "Which of these small bowls should I pour into the pan?"
  Assistant: highlights that bowl on screen and says "This one."
  Requirement: the highlight lands on the right object. For "which one" questions, prefer the highlight and keep speech short.

## 3. Functional requirements

- **F1 Wake**: during cooking, respond only when called ("Gemini"). No unprompted talk.
- **F2 Identify the dish**: infer the dish from the frame; when unsure, confirm verbally instead of guessing.
- **F3 Recipe search**: Gemini searches the web and returns several candidates (title, source link, ingredients, step summary).
- **F4 Show and confirm**: display a candidate and ask by voice; "no" moves to the next one, "yes" goes to F5. When candidates run out, search again and tell the user.
- **F5 Read the recipe**: put the confirmed recipe page text into context, then close the display. Later answers follow this recipe.
- **F6 Frame on demand**: every help request takes a fresh frame before answering.
- **F7 Voice answers**: short and conversational, one action at a time.
- **F8 Visual highlight**: output a box for the named ingredient, pan or bowl and display it; it clears after a few seconds or when replaced.
- **F9 Language**: Chinese and English, following the language the user speaks.
- **F10 Safety**: be conservative on doneness (cook longer when in doubt); say so plainly when the view is unclear.

## 4. Architecture

```
Glasses (mic / speaker / camera / screen)
        │  device adapter (the only module that touches hardware)
        ▼
Session orchestrator (state machine: idle → searching → awaiting confirmation → cooking)
        │  tool calls
        ▼
Gemini (voice dialogue + image understanding + web search + box coordinates)
```

Tools the orchestrator exposes to Gemini:

| Tool | Purpose |
|---|---|
| `capture_frame()` | Get the current camera frame |
| `search_recipes(dish, exclude)` | Web search, returns candidates |
| `show_recipe(recipe_id)` / `hide_recipe()` | Show / close the recipe display |
| `load_recipe(recipe_id)` | Fetch the recipe page text into context |
| `highlight(box, label, ttl)` / `clear_highlight()` | Highlight one region of the view |

What code can decide, code decides: state transitions, candidate de-duplication, the "load the recipe before closing the display" order, and highlight timeouts are enforced by the orchestrator, not left to the model.

The device adapter has two implementations: real glasses, and replay (used by the Harbor evaluation, section 5). The orchestrator and Gemini code are shared by both.

## 5. Evaluation harness: Harbor (hard requirement)

All evaluation runs through `harbor run`, following the neighbouring project `junction2026_Marie_Curie`.

Each scenario is one Harbor task directory:

```
tasks/<scenario>/
  instruction.md              the assistant's task statement
  task.toml                   declares the kitchen MCP server
  environment/Dockerfile      assistant container: holds no scenario answers
  environment/docker-compose.yaml   adds the kitchen sidecar
  environment/kitchen/        sidecar: replays the scenario (frames + user utterances), serves the section 4 tools, records the trajectory
  tests/test.sh               verifier: fetches the server-side score with a secret token
  solution/                   oracle: a reference policy that acts from the annotations
```

- The sidecar plays user utterances and frames along a timeline. Annotations (the correct object box, the recipe step the user should be on, whether the food is cooked) live only in the sidecar; the assistant container cannot see them.
- Each scenario is run with four policies: oracle (should score near full marks), do-nothing (should score near zero), an early-stop baseline, and the assistant under test N times. The first two show that the scoring can both pass and fail.
- Model output is stochastic: run each scenario several times and report every run, not only the mean. Failed and skipped scenarios are listed by name.

Scoring (decided by code wherever possible):

| Item | How it is judged |
|---|---|
| Flow | After a "no", was a different recipe shown; after "yes", did `load_recipe` come before `hide_recipe`; did `capture_frame` precede each help answer |
| Highlight | Which annotated object does the highlight box overlap most, and is that the correct one (a logical test, no tuned threshold) |
| Spoken content | Does it match the annotated facts (e.g. "meat not cooked → wait → then soy sauce"). Code cannot judge this, so an LLM judge does; the judge prompt is stored with the results |
| Response time | From the end of the user's utterance to the assistant's first spoken word: 3 seconds or less passes. Slow operations such as recipe search get a spoken acknowledgement first ("let me search") and then run. Every measured value is reported |

Not covered by Harbor: real microphone capture, the wake word, Bluetooth latency, and how the glasses screen actually looks. These are checked with a manual checklist on the device and recorded separately from Harbor scores.

## 6. Constraints

- The model is Gemini. The API key comes from the `GEMINI_API_KEY` environment variable and is never written into code or the repository.
- The exact model id is a configuration value.
- Every paid call (Gemini, LLM judge) is priced before it runs: calls per scenario × runs × scenarios.
- Results are recorded with their conditions: hypothesis, what changed, scenario ids, model, number of runs, commit.

## 7. Decisions

1. **Glasses model**: Meta Ray-Ban Display (with a screen).
2. **Screen output**: the glasses screen is available for display; the recipe and highlights are shown on the glasses.
3. **Response-time target**: 3 seconds or less from the end of the user's question to the first spoken word. Harbor measures this in the replay environment, without real microphone capture or Bluetooth latency; on-device time is measured separately in the manual check.
4. **Recipe sources**: any site.
5. **Scenario footage**: first-person cooking videos from YouTube, for example https://www.youtube.com/watch?v=KUzYFMgWs4w .
   - The repository stores only video ids, timestamps, scripted user questions and annotations, not video files or frames; footage is fetched by a local script and used for internal evaluation only.
   - The videos contain no user questions, so we insert them at chosen timestamps ("what next", "which bowl").
   - The correct answer comes from what happens next in the video: what the cook actually does after the question point (which bowl is poured, when the soy sauce goes in) is the annotation. Object boxes are annotated by hand.
6. **Form of the highlight**: the glasses screen is a small display at the side of the view, not an overlay aligned with real objects. Build it as a snapshot of the current frame with a box drawn on it; it may be extended later.
7. **No recipe translation**: the recipe goes into context in its original language, and answers follow the language the user speaks (F9).
8. **Get it running before evaluating**: first get the assistant and one Harbor scenario working end to end; the number of scenarios and of question points per dish is decided after that.
