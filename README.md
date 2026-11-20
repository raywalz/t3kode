# t3-koreader

An e-ink client for [T3 Code](https://github.com/pingdotgg/t3code): browse threads,
read conversations, and reply from a KOReader device (Kobo, Kindle, etc.).

Status: experimental.

## Pieces

- **Bridge** (runs next to the T3 server): talks to T3's API and serves a small,
  e-ink-friendly API to the device. Also generates suggested replies.
- **`t3.koplugin`** (runs in KOReader): thread list, conversation view, tap-to-reply
  (multiple choice), optional on-screen keyboard.
