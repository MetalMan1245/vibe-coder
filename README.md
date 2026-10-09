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
Conversations are saved, but the QActions import was broken, in addition to needing a QMenu and QMouseEvent to launch.  Currently the application crashes when clicking a conversation:

  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 255, in mousePressEvent
    if event.button() == Qt.MouseButton.LeftButton and event.source() != Qt.MouseEventFlag.NonMouseEvent:
                                                       ^^^^^^^^^^^^
AttributeError: 'QMouseEvent' object has no attribute 'source'

Crash when renaming a conversation:    
File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 519, in _show_context_menu
    self._rename_conversation(widget.conv_id)
    ~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^
  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 535, in _rename_conversation
    self.conversation_mgr.save_conversation(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        conv_id, self.history, new_title.strip()
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "/home/f/Dev/vibe-coder/vibe_coder/conversation_manager.py", line 96, in save_conversation
    "created": conv_data["created"],
               ~~~~~~~~~^^^^^^^^^^^
KeyError: 'created'

Also crashes when the Assistant's message finishes printing again:

  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 610, in _on_done
    self._finalize_assistant()
    ~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 638, in _finalize_assistant
    self._auto_save_conversation()
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 475, in _auto_save_conversation
    self.conversation_mgr.save_conversation(self.current_conv_id, self.history)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/f/Dev/vibe-coder/vibe_coder/conversation_manager.py", line 96, in save_conversation
    "created": conv_data["created"],
               ~~~~~~~~~^^^^^^^^^^^
KeyError: 'created'

Crash when creating a new conversation:

  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 437, in _new_conversation
    self._auto_save_conversation()
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/f/Dev/vibe-coder/vibe_coder/gui.py", line 475, in _auto_save_conversation
    self.conversation_mgr.save_conversation(self.current_conv_id, self.history)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/f/Dev/vibe-coder/vibe_coder/conversation_manager.py", line 96, in save_conversation
    "created": conv_data["created"],
               ~~~~~~~~~^^^^^^^^^^^
KeyError: 'created'
