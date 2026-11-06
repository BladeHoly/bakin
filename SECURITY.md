# Security

## What Bakin keeps

* **No passwords and no accounts.** Wallhaven works without a key (public,
  SFW and «sketchy» content only, never NSFW). An **API key is optional**
  (to list and follow your own collections): it is kept in the Windows
  Credential Manager (`bakin-wallhaven`), never in a file, a log or the
  window; it is sent only to wallhaven.cc's API (`X-API-Key`), never to its
  image servers, and the log redactor scrubs it. «Quitar la clave» deletes it.
* **Cookies only if you choose them**, for the yt-dlp presets that need them
  (a `cookies.txt` file you export, or a browser profile). Bakin reads them
  where they are and passes them to yt-dlp; it never copies them into its
  data folder, the downloads, logs, error messages or the window. It only
  reads their names, domains and expiry to warn when they stop working.
* Nothing is sent to any third-party service: Bakin talks to the sites you
  download from (through yt-dlp, and wallhaven.cc's API), GitHub for its
  own updates and tools, and nothing else.

## Logs

Logs are written to `%LOCALAPPDATA%\BakinData\logs\bakin.log`, outside the
repository. Every log record, including third-party library logs and
exception tracebacks, goes through a redactor that removes cookie values,
tokens and anything shaped like `Cookie:` or `Authorization:` headers.

## Things Bakin will not do

Circumvent MFA, CAPTCHAs, bot checks or rate limits; rotate IPs or proxies;
spoof fingerprints; store passwords. When a site says to slow down (429, «not
a bot» checks), Bakin stops and waits instead of retrying.

## Updates

In-app updates are signed: Bakin checks the signature before installing.
