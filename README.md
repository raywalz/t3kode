# t3-koreader

An e-ink client for [T3 Code](https://github.com/pingdotgg/t3code): browse threads,
read conversations, and reply from a KOReader device (Kobo, Kindle, etc.).

Status: experimental.

## Pieces

- **Bridge** (runs next to the T3 server): talks to T3's API and serves a small,
  e-ink-friendly API to the device. Also generates suggested replies.
- **`t3.koplugin`** (runs in KOReader): thread list, conversation view, tap-to-reply
  (multiple choice), optional on-screen keyboard.

## Running the bridge

Needs Python 3.12 and a running T3 Code server. There are no dependencies.

```sh
cp .env.example .env    # fill in T3_TOKEN, BRIDGE_TOKEN, and optionally SUGGEST_*
cd bridge && python3 -m t3bridge
```

Try it from the same machine:

```sh
curl -H "authorization: Bearer $BRIDGE_TOKEN" http://127.0.0.1:8787/v1/threads
```

To keep it running, install it as a systemd user service; see the comments at the
top of [contrib/t3-bridge.service](contrib/t3-bridge.service).

The device API is described in [docs/bridge-api.md](docs/bridge-api.md). Run the
tests with `cd bridge && python3 -m unittest discover -s tests`.

## Installing the KOReader plugin

Copy `plugin/t3.koplugin` into KOReader's `plugins/` folder (over USB or SSH) and
restart KOReader. Then open **Tools → T3 Code** and set **Bridge address**
(for example `http://<bridge host>:8787`) and **Bridge token** (the
`BRIDGE_TOKEN` from `.env`). The bridge must listen on an address the e-reader
can reach; see `BRIDGE_HOST` in `.env.example`.

You can also write the settings file directly to
`koreader/settings/t3.lua`:

```lua
return {
    ["bridge_url"] = "http://<bridge host>:8787",
    ["bridge_token"] = "<BRIDGE_TOKEN>",
}
```

## Trying the API test

A standalone check of T3's own API, without the bridge:

```sh
python3 bridge/api_test.py                         # read-only: lists threads
python3 bridge/api_test.py --project <id|title>    # creates, messages, reads, deletes a test thread
python3 bridge/api_test.py --project <id|title> --requests   # also answers an approval and a question
```

The write test only ever messages the thread it creates. Protocol notes are in
[docs/t3-api.md](docs/t3-api.md).
