# WhatsApp templates for Meta submission — 30 Days 30 AI Tools launch voice

Submit all six in **Meta Business Manager → WhatsApp Manager → Message templates
→ Create template**. Category **UTILITY** on every one. Language: English.
Approval usually takes one to two days.

The voice, baked into every template: greeting by name — "Hi {{1}}", never
"Namaste", because the students are young and many are Gen Z (Namaste/Dear is
reserved for faculty messages) — simple English, always encouraging — never a
threat, never shouting — and a proper closing. **No placement or internship language
anywhere: this course does not offer them.** No emojis.

The template **names must match exactly** — `delivery.py` refuses to queue a
message whose template name it does not know. Variables are positional and the
agent fills them in the order listed, 40 characters max each. **{{1}} is always
the student's first name**, resolved from the contact record.

---

## 1. class_starting_soon

Variables: 1 name, 2 course, 3 minutes, 4 join URL.

```
Hi {{1}}! Your {{2}} class starts in {{3}} minutes. See you there: {{4}} - Team Upskillize
```

## 2. deadline_final_hours

Variables: 1 name, 2 title, 3 hours left, 4 URL.

```
Hi {{1}}. Your task {{2}} closes in {{3}} hours. Please submit whatever you have ready - work submitted on time counts fully. You can do this: {{4}} - Team Upskillize
```

## 3. deadline_missed

Variables: 1 name, 2 title, 3 mentor name.

```
Hi {{1}}. The deadline for {{2}} has passed, but please do not worry. Talk to {{3}} - some submissions can still be accepted late. We are with you. - Team Upskillize
```

## 4. activity_expiring

Variables: 1 name, 2 activity, 3 hours left, 4 percent done, 5 resume URL.

```
Hi {{1}}. Your {{2}} is {{4}}% complete and closes in {{3}} hours. Your answers are safely saved - a short sitting finishes it: {{5}} - Team Upskillize
```

## 5. attendance_critical

Variables: 1 name, 2 misses, 3 course.

```
Hi {{1}}. You have missed {{2}} classes in {{3}} - those tools are pending for you. Every recording is saved, and your mentor is ready to plan the catch-up with you. Starting today makes it easy. - Team Upskillize
```

## 6. certificate_ready

Variables: 1 name, 2 certificate name, 3 claim URL.

```
Hi {{1}}! Congratulations - your Industry-Validated Certificate for {{2}} is ready. Claim it and share it proudly: {{3}} - Team Upskillize
```

---

## After approval

Nothing to deploy — `WHATSAPP_TEMPLATES` in `app/services/delivery.py` already
matches these names and variable orders, and resolves {{1}} from the contact's
full name. What remains on the LMS side is the `/outbox/whatsapp` drainer
(mirror of the email drainer) plus phone numbers with `whatsapp_opt_in` on the
contact records — contacts were synced with WhatsApp consent off, so nothing
sends until numbers and consent are loaded deliberately.

If Meta rejects a template: URLs must be on your verified domain, the body must
not read promotional ("offer", "unlock", "don't miss out"), and a variable must
not be the very first token — every template here opens with the word
"Hi", which also satisfies that rule.
