# Žarnovica website monitor (Railway + Resend)

A single, short-lived Python job. Checks **Podujatia**, **Oznamy**, and **Úradná tabuľa**. Emails each newly discovered item with its headline, up to 500 characters of article text, and a link to the article.

- **Oznamy**: official Atom feed (`get_rss.php?id=1_atom_2637`)
- **Úradná tabuľa**: official Atom feed (`get_rss.php?id=1_atom_2636`)
- **Podujatia**: event links on `https://www.zarnovica.sk/mid/466710/ma0/all/.html`

## Deploy to Railway

1. Place these files in a GitHub repository. In Railway, create a new service from the repository.
2. In its **Variables**, set:

   | Variable | Example | Meaning |
   | --- | --- | --- |
   | `RESEND_API_KEY` | `re_...` | Resend API key |
   | `EMAIL_FROM` | `Žarnovica Monitor <alerts@your-verified-domain.sk>` | Verified Resend sender |
   | `EMAIL_TO` | `alice@example.com,bob@example.com` | Recipients, separated by commas |
   | `STATE_FILE` | `/data/seen.json` | Optional; default already `/data/seen.json` |

3. Add a **Volume** to the service, mounted at exactly **`/data`**. This is needed to remember sent items after cron executions/redeployments. No database needed.
4. Set **Start Command** to `python app.py` (if Railway doesn't detect it automatically).
5. Configure **Settings → Cron Schedule**: `0 6,12,18 * * *`. Railway uses **UTC**, so in Slovakia this is around **07:00, 13:00, 19:00 in winter** and **08:00, 14:00, 20:00 in summer**. Execution time can vary slightly.
6. Deploy. The **first successful run** records existing items without emailing old publications. Subsequent runs send each unseen item once.

## Details

- No web server, no public domain, no frontend, no SQL database.
- Each notification is a separate email. A successful Resend API response is required before recording an item as sent.
- Resend's idempotency key reduces accidental duplicate sends on retries.
- If one source fails, the job logs an error and retries at the next scheduled run. It doesn't mark the failed source as checked.
- Detection is based on **new article URLs**. It intentionally does not email again on edits to already published articles.
- Source list/feed retention is finite: a post that appears and disappears between checks can be missed.
- RSS and HTML parsers are specific to the observed URL formats of Žarnovica. Test against the live site after deployment; the source website may change its markup or feed structure.
- The article text extractor is best-effort. Some items may show less than 500 characters if the source contains little extractable text.
- `EMAIL_FROM` must be on a domain authorized for sending in Resend. Do not use a generic personal Gmail sender.

## Local dry run

Create a local state file, e.g. `STATE_FILE=./seen.json python app.py`, to verify RSS and event parsing. With a missing state file, the first run does **not** call Resend. For the production job, keep the state on the attached Volume.

## Privacy and maintenance

The state file stores only article URLs. No published content or recipient list is saved in the state. Recipient addresses and Resend key remain Railway variables.
