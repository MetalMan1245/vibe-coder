# vibe-coder
 A local code editor with simple setup and a local AI first workflow.
 
 Note: This repository is extremely new, and as such is currently only a scaffolding for the application described.  It does work but is not what I want it to be yet, stay tuned for more updates!
 
# Architecture
A native Linux application designed for vibe coding small, self contained projects like the ones I work on.  It should be local first, designed to work with an ollama hosted model.

Phase 1:  A simple interface for talking with a locally hosted AI agent, similar to Lumo, ChatGPT or any LLM interface, with settings for where the server is accessible, a system message (like: My OS is CachyOS unless otherwise specified) and preferably memories that live in ~/.config/vibe-coder if that will work well on a locally hosted AI model.

Phase 2: File access.  The user can create a project that the AI model has access to (preferably git aware so the model can make git commits with automatic summaries).  Clear documentation of what code changes the AI is making as well as a log in the project directory at ./.vibe-coder.

Phase 3: Polish and configuration.  Testing edge cases, potentially building for Windows and Mac, and making it possible for the app to auto detect the user's hosted LLM server, or set one up for them from scratch (download and run Ollama with system start or start with app, configuring LLM and auto recommendation of an appropriate model based on hardware scan).

QScintilla or QTextEdit for text framework.

# Warning
This is vibe coded software intended for other vibe coders who otherwise don't know what they're doing (I sure don't) as such I do not guarantee that nothing will go wrong or cause any issues.  However feel free to submit an issue report if something does happen, or if you're someone who does know what you're doing feel free to submit a pull request!

# Dev
Git commits still report failure even though they seem to be working just fine.
/home/f/.config/vibe-coder/conversations/fa341ece-f29c-4e27-9df3-702169f8b69c.json

Cases where the  conversation can extend to needs a horizontal scroll bar, there should never be a horizontal scroll bar under any circumstances.
/home/f/.config/vibe-coder/conversations/fa341ece-f29c-4e27-9df3-702169f8b69c.json
/home/f/.config/vibe-coder/conversations/c7eb7bb5-23d1-4cb1-baf5-df528c6ec167.json
/home/f/.config/vibe-coder/conversations/d8ec23f1-e363-43ee-a601-a13e40b05951.json

Display for context usage for the selected model and ability to alter ollama parameters graphically if possible.

There should be a persistent indication of the active project (hidden if no project active) in the conversations tab so that the user does not have to switch back to the Projects tab to check the active project.

Run script for easy execution.

Change project name to Monkey instead of vibe coder.

Change advanced settings toggle to a different tab in the settings dialog.

LLM web extract, web search, other useful coding tool usage.

Move Settings to top right, status indication to where settings is, directly above chat box.

Default user system message:  My OS is <fill in user's linux distro> unless otherwise specified.
