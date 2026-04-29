## Flutter plan: prikaz prijevoda mailova (OpenAI backend)

Ovaj dokument opisuje što Flutter frontend treba implementirati kako bi koristio backend endpoint za prijevod poruka:

- `POST /api/mail/messages/<uid>/translate`

Backend vraća **plain text** prijevod (ne HTML), uz metadata (source/target language, cached, truncated, model).

### 1) Modeli (DTO) i mapiranje

- **Dodati DTO** za response prijevoda:
  - `account_email: String`
  - `folder: String`
  - `uid: String`
  - `message_id: String`
  - `target_language: String`
  - `source_language: String`
  - `translated_subject: String`
  - `translated_text: String`
  - `cached: bool`
  - `truncated: bool`
  - `model: String`

- **Dodati DTO** za request:
  - `folder: String` (default `INBOX`)
  - `target_language: String` (default `hr`, ali čitati iz app postavke ako postoji)

### 2) API klijent

- Implementirati metodu npr. `translateMessage({required String uid, required String folder, required String targetLanguage})`.
- Koristiti isti auth kao postojeći mailbox API pozivi (Token auth).
- Timeout na klijentu postaviti na **>= 60s** (jer prijevod može trajati 20–40s).

### 3) State management (po poruci)

- U “message detail” state dodati:
  - `translationStatus`: `idle | loading | ready | error`
  - `translation`: `MessageTranslationResponse?`
  - `translationErrorCode`: npr. `"translation_unavailable" | "translation_failed" | "mailbox_credentials_missing" | ...`

- Cacheiranje na strani appa (opcionalno):
  - Minimalno: držati prijevod u memoriji dok je screen otvoren.
  - Opcionalno: persistirati po ključu `(account_email, folder, uid, target_language)` uz TTL.

### 4) UX / UI ponašanje

- U detail header (ili action menu) dodati:
  - **Toggle**: “Prikaži prijevod”
  - ili **Button**: “Prevedi”

- Kad korisnik uključi prijevod:
  - Ako `translation == null`: okinuti API poziv i prikazati loading.
  - Ako `translation != null`: samo prebaciti prikaz.

- Prikaz statusa:
  - Subtitle/chip: `Translated to <target> · from <source> · cached` (kao na screenshotu)
  - Ako `truncated == true`: prikazati “Skraćeno zbog duljine poruke”.

### 5) Prikaz tijela poruke (ključno)

Newsletteri i mnogi mailovi su HTML; backend prijevod vraća **plain text**.

- Ako je “Prikaži prijevod” aktivan i `translationStatus == ready`:
  - **ne renderati originalni HTML**
  - umjesto toga prikazati `translation.translated_text` kao plain text (scrollable/selectable)

- Ako prijevod nije aktivan:
  - prikazati original (postojeći HTML renderer / original body)

Preporuka:
- Za plain text koristiti monospace nije potrebno; radije obični font, line-height malo veći, i omogućiti copy/select.

### 6) Target language (odabir jezika)

- Minimalno: hardcode `hr`.
- Bolje:
  - u Settings ekranu dodati “Jezik prijevoda” (dropdown)
  - spremiti u lokalne postavke
  - koristiti kao `target_language` u requestu.

### 7) Error handling (što prikazati korisniku)

Mapiranje backend grešaka:

- `503 translation_unavailable`
  - poruka: “Prijevod trenutno nije dostupan.”
- `502 translation_failed`
  - poruka: “Prijevod nije uspio. Pokušaj ponovno.”
- `400 empty_message_body`
  - poruka: “Nema sadržaja za prevesti.”
- `401 mailbox_credentials_missing`
  - poruka: “Nedostaju mailbox podaci. Ponovno se prijavi.”

UI:
- U error stanju ponuditi **Retry**.

### 8) Observability (debug)

- Logirati:
  - uid/folder/target_language
  - trajanje API poziva
  - `cached`/`truncated`
  - error code (ako padne)

### 9) Test plan (ručno)

- Otvori poruku (UID) i klikni “Prevedi”.
- Provjeri da se body prebaci na plain text prijevod.
- Ponovi prijevod (isti uid) i provjeri da UI pokaže `cached` i da je response brži.
- Probaj veliki newsletter (očekuj da izgled neće biti “identičan” jer je prijevod plain text).
- Simuliraj `OPENAI_API_KEY` prazno → treba dobiti `translation_unavailable`.

