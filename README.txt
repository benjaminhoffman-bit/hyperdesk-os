HyperDesk OS by SilvaDes
========================

HyperDesk OS is a free AI desktop that runs on your own PC. The models run on
this computer, so your conversations stay on it.


What you need
-------------
- Windows 10 or 11, 64-bit.
- About 15 GB of free disk space.
- An NVIDIA graphics card with 8 GB or more of video memory is recommended.
  It works without one, just slower.


Install
-------
1. Right-click the zip and pick Extract All.
2. Double-click Install.cmd.
3. Windows may warn that the publisher can't be verified. That is because the
   script isn't code-signed. Click Run (or More info, then Run anyway).

The installer sets up Python 3.12 and Ollama for your Windows user only,
through winget, and accepts their license terms for you. It needs no admin
rights. Then it downloads one model of about
7 GB, makes your shortcuts and opens HyperDesk. It installs into
%LOCALAPPDATA%\HyperDesk.


What works out of the box
-------------------------
- Chat with the local model.
- Documents: attach a PDF, Word, Excel or PowerPoint file and ask about it.
- Knowledge: notes you save, which HyperDesk uses in its answers.
- Post trends (Analytics page): pick a CSV of your posts exported from
  TikTok Studio, YouTube Studio or Meta Business Suite to see which
  hashtags, days and times did best. The file stays on this PC.
- Game Mode (Start menu): before a game, asks the apps you tick to close
  (each one can ask you to save first) and stops the local AI.

Optional, if you want them:
- Claude Code, if you have a Claude plan. Install it and sign in, and you
  can pick Claude for bigger questions. Auto never sends anything to it.
- A free cloud tier: get a free Groq API key (console.groq.com) and save it
  in Apps & APIs, Free cloud. For another OpenAI-compatible provider, change
  url and model under [free] in
  %LOCALAPPDATA%\HyperDesk\AgentOS\config.toml.
- A Gemini key and model ID (from Google AI Studio), also in Apps & APIs.
- Quick edit (Video page): cuts the pauses out of a recording, plus typed
  edits like "trim start 5" or "cut 2:10-2:40". No AI. It needs FFmpeg:
  run  winget install Gyan.FFmpeg  then reopen HyperDesk.
- Lights (Start menu): one color for your RGB, through OpenRGB
  (openrgb.org). In OpenRGB's SDK Server tab keep host 127.0.0.1 and
  port 6742, click Start Server, then Rescan in Lights.


The ads
-------
This free build shows small ads for SilvaDes, my business. They are built in:
nothing is downloaded or stored for them. A click opens silvades.com, or
SilvaDes on TikTok, in your browser. The silvades.com links tell the site they
came from HyperDesk and which ad, nothing about you.


Uninstall
---------
Start menu, Uninstall HyperDesk. Or run Uninstall.cmd in %LOCALAPPDATA%\HyperDesk.
It removes HyperDesk and everything in that folder, including the files and
videos it made in workspace\files and workspace\videos. It tells you how to remove Ollama, Python and the
model too, if nothing else uses them.


License
-------
Free to download, install and use. Please don't sell it or share changed
copies. The details are in LICENSE.txt. Newer builds and the source are at
github.com/benjaminhoffman-bit/hyperdesk-os


Contact
-------
silvades.com
benjaminhoffman@silvades.com

Build 2026-10-24
