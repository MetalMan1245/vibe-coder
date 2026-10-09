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
Add code block formatting if possible with Ollama output.  If not, it is time for phase 2, with structured "projects" with file access in a certain scope defined explicitly by the user.  Git access is preferable, should be able to generate a new project, initialize Git with a README and the AI should make changes directly to files in the scope without asking, warn the user the first time they make a project to frequently back up the location since the AI does not ask before making changes
