# Promotion: automated Instagram carousels

Posts one carousel per day per account using the official Instagram API.
GitHub Actions runs it for free; Instagram fetches the slides from this public repo.

## Layout
```
accounts/<account>/config.toml           start date, time zone, post hour
accounts/<account>/posts/001/1.jpg ...   2–10 JPEG slides, 1080x1350 (4:5)
accounts/<account>/posts/001/caption.txt caption + hashtags
accounts/<account>/posted.json           written by the bot; don't edit
```
Posts go out in folder order (001, 002, ...). Slides go in number order (1.jpg, 2.jpg, ...).

## One-time setup per account
1. Switch the Instagram account to Creator or Business.
2. In your Meta developer app, add the account as an Instagram tester, accept the invite
   in Instagram, and generate a token with `instagram_business_basic` and
   `instagram_business_content_publish`.
3. Repo **Settings → Secrets and variables → Actions**, add:
   - `IG_USER_ID_<FOLDER>` and `IG_TOKEN_<FOLDER>` (e.g. `IG_TOKEN_VINTAGE`)
4. For a new account, add its two lines to `post.yml` and its token line to
   `refresh-tokens.yml`.

## Token auto-refresh (one time)
Create a fine-grained personal access token limited to this repo with
**Secrets: Read and write**, and save it as the secret `GH_PAT`.

## Test before going live
Actions → **Post to Instagram** → Run workflow with *dry run* checked.
Locally: `pip install -r requirements.txt && python scripts/publish.py check`
(flags non-JPEGs, including PNGs renamed to .jpg, and slides outside 4:5 to 1.91:1).
