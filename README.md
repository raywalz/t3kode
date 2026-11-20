# t3-koreader

An e-ink client for [T3 Code](https://github.com/pingdotgg/t3code): browse threads,
read conversations, and reply from a KOReader device (Kobo, Kindle, etc.).

Status: experimental.

## Pieces

- **Bridge** (runs next to the T3 server): talks to T3's API and serves a small,
  e-ink-friendly API to the device. Also generates suggested replies.
- **`t3.koplugin`** (runs in KOReader): thread list, conversation view, tap-to-reply
  (multiple choice), optional on-screen keyboard.

## Trying the API test

Needs Python 3.12 and a running T3 Code server. There are no dependencies.

```sh
cp .env.example .env    # then fill in T3_TOKEN (see the comment in the file)
python3 bridge/api_test.py                         # read-only: lists threads
python3 bridge/api_test.py --project <id|title>    # creates, messages, reads, deletes a test thread
python3 bridge/api_test.py --project <id|title> --requests   # also answers an approval and a question
```

The write test only ever messages the thread it creates. Protocol notes are in
[docs/t3-api.md](docs/t3-api.md).
