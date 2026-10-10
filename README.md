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
Give user access to the crrently system message in settings with a warning that is is not advised to change it, separate from the user's system message.

Still no ability to deselect project and chat with no project.

Encountered a bug where deleting all conversations repopulated the old conversations with the title "New Conversation" on all of them, deleting again seems to have solved it.

Make the index of conversation titles human readable instead of the current random string of characters so that conversation parsing is easier.
