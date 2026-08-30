# Meeting Recorder for account managers

Written for the people who own the relationship and the number. If you
build the solution rather than sell it, `docs/ai-integrations.md` and
the delivery templates are the ones you want instead.

---

## 1. Pick the right template before you process

The template decides what survives. Run a pricing call through the wrong
one and you get a faithful record of the architecture and no trace of
the discount that was offered out loud.

| Your meeting | Template | What it protects |
| --- | --- | --- |
| First real conversation with a prospect | **Qualification Call** | budget authority, compelling event, competitor named, and — deliberately — the qualifying questions that went *unanswered* |
| C-level, 30 minutes, business case | **Executive Briefing** | which outcome the sponsor is measured on, who the sceptic was, what an exec committed to |
| Showing the product | **Solution Demo** | reaction per capability, objections verbatim, gaps acknowledged, "can it do X" left unanswered |
| Any conversation about money | **Pricing & Commercial** | every figure with what it covers, whether it was firm or indicative, and each concession with the condition attached |
| Existing customer, relationship check | **Account Review / QBR** | adoption vs. what they bought, risk signals, expansion signals, renewal dates |
| Displacing an incumbent | **Competitive Displacement** | contract end date, switching barriers, competitor claims worth verifying |
| Handing a won deal to delivery | **Sales-to-Delivery Handoff** | **what was promised verbally versus what the SOW covers** |

**Qualification Call is not Requirements Gathering.** The latter is an
SA template about what a system must do. Yours is about whether there is
a deal. They are kept separate on purpose.

**The handoff one earns its keep on its own.** Its job is to surface the
gap between what got said in the sales cycle and what got signed. That
gap is where delivery escalations come from, and it is invisible to
everyone except the person who was in both rooms.

Change the template on a session and reprocess it if you picked wrong —
nothing is lost.

---

## 2. Turn on the live co-pilot for the meetings worth coaching

Settings → Recording → **Live Co-Pilot**. It costs roughly $0.10–$0.20
per hour and prompts you every ~45 seconds with questions to ask, risks
it spotted, and follow-ups.

Set your **mode** to `Sales` once. Then set the **meeting type** per
call:

| Type | What it watches for |
| --- | --- |
| **Qualification** | a question you asked that never got answered, enthusiasm from someone with no authority, a timeline with no event behind it, you talking more than they are |
| **Pricing / Negotiation** | a concession offered with no condition attached, an indicative number being repeated back as firm, negotiating against yourself before they've countered |
| **Executive Briefing** | jargon creeping in, a business question answered technically, the sponsor going quiet, the meeting ending with no named next step |
| **Renewal / Account Review** | a complaint mentioned once and moved past, a sponsor who has changed, an expansion signal going unexplored, the renewal date nobody named |

The pricing lens is the one to try first. It is the meeting where a
sentence becomes a contractual position, and the one where a nudge in
the moment is worth most.

---

## 3. Ask your assistant about the account

Once the app is connected to Claude (see `docs/ai-integrations.md`),
these work in plain English. This is where the archive stops being a
filing cabinet.

**Before a call**

- *What did we agree with [client] about pricing, and when?*
- *Summarise every conversation with [client] in the last quarter.*
- *What objections has [client] raised, across all meetings?*
- *What did we promise [client] that we haven't delivered?*

**Chasing the deal**

- *What do I still owe anyone?* — overdue first, with the meeting to cite
- *What is [client] waiting on from us?*
- *Which of my accounts hasn't been touched in six weeks?*

**Writing something**

- *Draft a follow-up email to [client] from our last meeting.*
- *What should be in the SOW for [client] based on what we discussed?*
- *What did [competitor] get accused of in our calls this year?*

**Two habits worth forming**

Ask it to **cite the meeting**. It can, and a quoted commitment with a
date attached settles an argument that a paraphrase does not.

Ask it what it **doesn't** know. "What did we never get an answer to
from [client]?" is the most useful question on this page, and it is the
one nobody thinks to ask.

---

## 4. Two things that will bite you

**Only meetings are indexed, not your documents.** If your client
folders show 0 documents, search is running on transcripts alone — your
SOWs and proposals are invisible to it. Check Settings → Clients: a
Knowledge Folder pointing at the same path as its Export Folder is the
usual cause.

**A client name spelled two ways is two clients.** Every meeting tagged
to the misspelling is orphaned from the real account and will not show
up when you ask about it. Worth a glance down the client list before you
trust a "nothing found".
