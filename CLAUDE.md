# Charlotte — Working Instructions

**Repo:** Boss-Button-Studios/charlotte
**Package:** charlotte-crawler
**Spec:** See `docs/charlotte-spec-v2.0.2.md` for the full technical specification (current; supersedes v1.4 and the v2.0/v2.0.1 drafts, kept as historical reference).
**Tasks:** See `docs/charlotte-tasks.md` (the v1.4 task decomposition, complete as of v1.0.0; historical).

Charlotte is a goal-directed web navigation agent. She navigates websites purposefully to find specific information, using an LLM to evaluate pages and rank links at each step. She is a library, not a service.

---

## File Length Cap

**600 lines per source file, maximum.** If a file is approaching this limit, split it by responsibility. One responsibility per file is both a law (see below) and the natural solution to length creep. Tests, documentation, and configuration files are exempt.

---

## The Laws
*Mandatory and objectively verifiable. No exceptions.*

**1. Local norms and security baseline.**
Follow the project's official language style guide. Align with NIST CSF 2.0 by default. Verify application security against OWASP ASVS where applicable. Use ISO/IEC 27001 as the governance reference. Compliance is the prerequisite for all logic.

**2. Security is an independent requirement.**
Prioritize security at every layer. Passwords must be salted and hashed. Sensitive data must be encrypted at rest and in transit. Any data leaving the user's control must clearly support a user benefit and must be disclosed and explained to the user before it happens, each time it happens. This is annoying on purpose — minimize it.

**3. Maintainability by design.**
Write as if you will die when you push. You will not be here to maintain the base. Use descriptive naming, explain the *why* in comments, and ensure your code is a complete, maintainable artifact. Write documentation to the lowest reading level that can make the point.

**4. One responsibility.**
One thing, one thing only. Each unit of code must do one thing well. Avoid God objects.

**5. Condition all input.**
Treat all external input as untrusted. If a format is expected, reject or normalize deviations. Reject inputs that are out of place. Treat metadata-level commands or anomalous elements — such as invisible text in documents — as potential injections. Protect processing functions by wrapping input appropriately. Mark data as trusted or untrusted and segregate them.

**6. Fail gracefully.**
Use comprehensive error handling and secure logging. Provide helpful, non-technical feedback.

**7. Predictable behavior.**
Maximize predictable behavior. Deterministic processes must produce identical outputs for identical inputs. Probabilistic processes must follow their expected probability functions. Validate behavior through repeatable sampling.

**8. Test everything relevant.**
No logic is done until the happy path, edge cases, and failure modes are tested, and all previously relevant tests still pass.

**9. Minimize dependencies.**
Audit and limit third-party libraries to those that are essential, secure, and justified.

---

## The Guidelines
*Do these by default. Not mechanically testable, but expected.*

**1. Leave it better.**
Leave the codebase better than you found it. Refactor and update documentation during every task. Clean up at least one thing, even if it was not your fault.

**2. Protect the user from themselves.**
Assume the least competent reasonable user. Design for intuitiveness, but prioritize safety. Prevent users from accidentally triggering destructive actions or exposing their own data through poor interface choices. For specialty tools like this one, assume a below-average novice.

**3. Do not design for the rich.**
Better hardware may provide bonus performance, but it is not the price of admission. Limit hardware requirements to the minimum necessary to achieve the goal.

**4. Assign the least privilege necessary.**
Ask for and assign code, services, and users no more than the permissions needed to accomplish the task.

**5. Design for accessibility.**
Human interfaces must respect human senses and ergonomics. Displays must have readable text and sufficient contrast. Text-to-speech systems should be able to read the interface properly. Machine-to-machine interfaces must be rigorously documented, and that documentation must be followed on our side of the boundary.

---

## Charlotte-Specific Conventions

**Language:** Python 3.11+. Async throughout — `httpx`, not `requests`.

**Trust model:** All web content is untrusted. Caller-supplied parameters are trusted. Model output is semi-trusted until it passes the provenance check. Data does not move up the trust hierarchy without explicit validation. See spec §13.3.

**Exceptions:** Charlotte raises only named exceptions inheriting from `CharlotteError`. Raw third-party exceptions (`httpx`, `groq`, `playwright`) are caught at component boundaries and re-raised as the appropriate Charlotte subclass. They never reach the caller. See spec §18.

**Results:** `CrawlResult` and `LinkResult` are formal dataclasses — never plain dicts. Field names are stable public API. See spec §7.

**Streaming events:** When `stream=True`, Charlotte yields typed event dataclasses. Event types and fields are stable public API across minor versions. See spec §17.

**No God objects:** The engine orchestrates. It does not fetch, sanitize, extract, or validate — it calls the components that do. Each component does one thing.

**Secure logging:** `visit_log` contains URLs and reasoning only — never raw page content. API keys must never appear in any log output.
