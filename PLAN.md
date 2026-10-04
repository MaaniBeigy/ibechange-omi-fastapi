# Artemis updates and constrained conservative Double DQN

## Scope and contracts

- [x] Define typed envelopes for the nine documented messages and the three additional captured types: `task_step_scheduled`, `task_step_done`, and `mission_accomplished`. Parse one event payload per broker message, including its source process and mission identifiers.
- [x] Parse all 62 message bodies in `docs/artemis_example.json`. The file is a diagnostic transcript with embedded JSON bodies. Preserve psychological assessments that omit `user_uuid` as unattributed rows with their source payload and broker ID.
- [ ] Keep captured psychological assessments unattributed for now. Ask the Artemis producer for a reliable user identifier before using them in per-user policy state. The four captured payloads have no `user_uuid` or other labelled user identifier; retain their source bodies and message IDs for later assignment.
- [x] Use the latest ordered selected-content update from POLIMI CS as the queue at each decision epoch. Keep its order and duplicate occurrences when the same queue is received again. A later update may change the queue. The TU/e OMI service consumes its own Artemis queue.
- [x] Ingest each `task_step_scheduled` interval with its original start and end strings, update later schedule snapshots without letting older messages replace newer ones, and retain task completion and mission accomplishment events. Match a reminder by user UUID and mission ID and use the latest `endDate` on the local decision day. When no step ends that day, use the selected mission's global end time; if it is null, use mission start plus seven days. Recommendations and resources have no mission-time deadline.
- [ ] Confirm with Artemis whether offset-free `startDate` and `endDate` values use the local mission time zone. Serving currently uses `ARTEMIS_SCHEDULE_TIMEZONE` (default `POLICY_TIMEZONE`) and rejects ambiguous clock times for matching steps.
- [x] Validate a daily queue prefix with one to two reminders and one to two recommendation-class items (recommendations and resources combined) when each class is present, including sends already committed before a queue update. The constraint mask requires completion of that prefix when decisions run. Keep later queue occurrences for following days. Report an infeasible queue when its order, daily class budget, or reminder deadline prevents the supplied class minimum.
- [x] Fix the runtime decision contract: the RL policy chooses send or skip for the next item in an ordered queue at each decision epoch. Define and run the scheduler through every required epoch so daily minima are met in delivery, how far ahead `/notification_plans` may return committed sends, and how later `notification_sent` and `notification_opened` events update state without using future responses.
- [ ] Set an RCT policy version, fixed configuration, training data source, model artifact format, and rollout assignment rule. Record any permitted online fitting separately so treatment assignment and policy changes are auditable.

## Artemis ingestion and persistence

- [ ] Use the shared standalone `consumer.py` as the starting example for STOMP connection and message inspection. Its command `python consumer.py --queue int.ibechange.update::tue-updates-int` subscribes to `/queue/int.ibechange.update::tue-updates-int`; make the production queue name configurable and set it to the TU/e OMI queue in deployment. POLIMI CS produces the selected-content queue.
- [x] Add `stomp.py` to runtime dependencies and configure `ARTEMIS_HOST`, `ARTEMIS_PORT`, `ARTEMIS_USER`, `ARTEMIS_PASSWORD`, and the queue name through the environment. Keep the supplied 10-second STOMP heartbeats, reconnect after disconnects, and close the connection cleanly on shutdown.
- [x] Support optional `ARTEMIS_CA_CERT`, `ARTEMIS_CLIENT_CERT`, and `ARTEMIS_CLIENT_KEY` settings and require a CA when client certificates are used.
- [x] Use STOMP `client-individual` acknowledgement for the application consumer. The shared script uses `ack="auto"`, which acknowledges deliveries and can drain the queue despite its read-only comment; do not run it against the application queue when those events must be retained for the service.
- [ ] Keep a separate diagnostic command that prints destination and message ID headers plus parsed JSON, without logging credentials or private key material. Correct the shared script's separate listener instances so its shutdown count reflects received messages, and document that its disconnect callback alone does not reconnect.
- [x] Parse and validate each message with typed schemas in `app/schemas/update.py`. Preserve the original field names at the broker boundary and normalize UUIDs, timestamps, and notification content types in one place.
- [x] Move database changes out of `app/routers/updates.py` into a shared update service. Replace `/updates` ingestion with the Artemis consumer, remove the obsolete route from `app/main.py`, and update API documentation and tests.
- [x] Process a message in one database transaction and acknowledge it only after commit. Reject or quarantine invalid messages with a recorded reason; make redelivery and consumer restart safe through a durable idempotency key.
- [x] Implement `new_user` and `disabled_user` using the new nested user identity and `attributes` fields. Exclude disabled users from scheduling.
- [x] Store the new per-pillar health habit score, colour, trend, subpillars, and assessment timestamp. Store psychological barriers, subpillar, answer task log, and assessment timestamp without flattening away the source data.
- [x] Store `escalation_level` and `new_pillar` per user and pillar. Store each `mission_selected` occurrence with `process_id`, `mission_process_id`, mission code, and timestamp.
- [x] Store `notification_sent` and `notification_opened` as distinct events with their process IDs, mission process ID, content ID, content type, mission ID, and event time. Match opens to sends without counting duplicate deliveries; preserve out-of-order events until their match arrives.
- [x] Add five database migrations for new columns, tables, indexes, uniqueness rules, and nullable source fields. Test the captured-event migration against a database with the previous column contract.
- [x] Update `app/models/__init__.py`, application startup, deployment configuration, and example payloads for the consumer and migrations.

## Content and scheduling data model

- [x] Represent each selected content occurrence with stable order, content ID, type, mission code, and optional process identity. Derive reminder deadlines from same-day Artemis mission-step records or the selected mission time window and preserve duplicate content IDs as separate queue entries.
- [x] Accept `recommendation`, `resource`, and `reminder`. Keep resources in the recommendation RL class and retain their original type in persistence and API responses.
- [x] Record the reminder deadline and any queue-supplied mission process on each planned send. The queue process and step IDs do not affect deadline resolution. Report missing, expired, ambiguous clock-time, or infeasible deadlines.
- [ ] Record the Artemis task-window ID that supplied the same-day deadline on each planned reminder for audit.
- [x] Update plan persistence and response schemas so each scheduled row identifies its selected occurrence, original content type, mission process, deadline, policy version, and scheduled time. Add uniqueness rules that allow repeated content IDs but prevent duplicate scheduling of one occurrence.
- [x] Update `/recommendation_plans` and `/notification_plans` to return the revised model and maintain their current response contracts where fields still apply. Exclude disabled users and deleted selections.

## RL policy integration

- [x] Extract the required `con_cddqn` inference, state, delivery-mask, and configuration code from `/home/mbeigi/notification-reward-engineering` into a versioned, installable runtime dependency or an attributed local module. Keep the original equations and tests as the reference for changes.
- [x] Extend the two-class queue encoding to map both recommendations and resources to the recommendation class while retaining reminders as the deadline class. Check that the deployed state layout matches the trained model's input width, including wearable and non-wearable variants.
- [ ] Build observations from data available at decision time: local clock, day and week counts, time since last send, observed opening history, available wearable readings, mission schedule, and ordered remaining items. Define cold-start values and reset behavior at day and week boundaries.
- [x] Apply the source queue viability mask to both serving and Double DQN targets. Enforce quiet hours, spacing, daily and weekly caps, item order, and each item's deadline; report infeasible queues explicitly. Serving sets the daily total floor from the classes present in the ordered queue and the weekly floor to zero because POLIMI may supply fewer items than the simulator. Document this difference from the source training configuration.
- [x] Preserve the source algorithm's Double DQN action selection and target evaluation, elapsed-minute discount, conservative loss over feasible actions, and constrained engagement-plus-unopened-send reward. Verify these calculations against the RL repository's math and tests before changing its item encoding.
- [x] Train and export a constrained conservative Double DQN model and its preprocessing/configuration metadata. The RL repository contains training code but no saved model artifact in the inspected tree. Validate artifact version and state dimensions at startup.
- [x] Define the safe behavior for a missing or invalid artifact and for an infeasible queue. It must not silently restore random scheduling or claim a reminder deadline was met when it was missed.
- [x] Replace `generate_random_schedule` in `app/services/recommendation_plan_service.py` with one policy decision per epoch over the current day's ordered queue prefix. The router reads the latest POLIMI queue and records its send or skip action. Remaining occurrences can continue on later days.
- [x] Persist each decision's state, safe action set, action, propensity, policy version, and outcome link. Close the observation window before computing engagement and any online update; use `notification_sent` as delivery evidence and `notification_opened` as an observed response.
- [x] Make scheduling idempotent under repeated API calls, concurrent workers, and Artemis redelivery. Use database constraints and transaction boundaries for one send per selected occurrence.

## Tests, coverage, and formatting

- [x] Add schema and ingestion tests for all twelve supported Artemis messages, including every captured body, unattributed psychological assessments, malformed task intervals, schedule replacement, and replay.
- [x] Add consumer and service tests for STOMP destination construction, environment and TLS settings, connection recovery, transaction rollback, acknowledgement after commit, duplicate delivery, restart, invalid-message handling, out-of-order sent/opened events, and concurrent processing.
- [ ] Extend migration tests beyond the captured-event migration to cover old recommendations and resources, repeated mission selections, repeated content IDs, and rollback on failure.
- [x] Add RL unit tests for resource/recommendation equivalence, reminder deadlines, queue order, quiet hours, spacing, daily and weekly counts, infeasible queues, state field order, action masks, artifact compatibility, and deterministic decisions with a fixed model.
- [x] Add integration tests for selected content, same-day Artemis deadlines, mission end and seven-day fallback, snapshot replacement, both plan endpoints, disabled users, repeated requests, concurrent scheduling, and feedback-driven state changes. Replace tests and fixtures that depend on the old batch `/updates` payload or random schedule.
- [x] Measure application line and branch coverage and bring both close to 100% with meaningful tests. See the 2026-10-04 verification results below for the current measured coverage.
- [x] Use fixtures and mocks for consumer failures, transactions, message validation, deadline matching, and policy decisions. Only command-line `__main__` guards are excluded; the actual main functions are tested. Two defensive branches remain uncovered and are not excluded: a second time-zone round-trip check after the offset check, and the fall-through from the final event dispatch condition after a known event has been parsed.
- [x] Add `black` and `isort` to development requirements, run their checks on changed Python files, and run the full test suite with branch coverage.

## Completion checks

- [ ] Verify every new user and notification message can be consumed from the OMI queue and replayed without duplicate rows or sends.
- [ ] Verify resources receive the same scheduling constraints as recommendations, every reminder meets its available hard deadline, and infeasible cases are reported.
- [x] Verify no production path calls `generate_random_schedule`, no policy decision uses an opening that was not yet observed, and model/configuration versions are recorded with each decision.
- [ ] Update the data-model diagram, environment example, deployment instructions, and API examples to match the delivered implementation.

## Verification so far

The Artemis consumer, update service, schema, observation, and policy tests pass
against PostgreSQL. The full suite passed on 2026-09-29 with 164 tests and 99.87%
combined line and branch coverage. The capture tests processed all 62 embedded
messages and checked replay without extra rows. The captured-event migration was
checked against the prior column contract. Remaining work includes confirmation
of the producer's time-zone convention, a user identifier for captured
psychological assessments, and live Artemis verification after deployment.

## RL review implementation — 2026-10-04

The complete finding-by-finding assessment is in
[docs/review-verification.md](docs/review-verification.md). The original exporter
already implemented offline training; this change supplies the missing live
learner and delayed-feedback transition pipeline. The source NumPy network is
attributed locally, and reference update parity covers Double DQN, discounting,
CQL, Adam and target copies.

`POLICY_LEARNING_MODE=online` fits each user's own stream; `frozen` records the
same audit with fixed artifact weights. PostgreSQL stores replay and full
checkpoints. A dedicated worker runs real grid epochs; REST can commit its next
grid epoch using information available at request time. Existing delivery
integrations still retrieve and deliver committed plans. Daily queue termination
and deterministic acting are documented serving adaptations.

Compose runs bootstrap/migrations, ingestion, API and worker. A separate `checks`
profile runs formatting and branch coverage against a disposable test database.
The new migration is tested in both directions and preserves existing decisions.
Fresh bootstrap through the full migration chain was also run against PostgreSQL.
The RL persistence supplement is [docs/rl-data-model.md](docs/rl-data-model.md).

Unresolved producer/trial contracts remain open above: psychological user IDs,
Artemis time-zone conventions, trial assignment/version rules, live broker
verification and calibrated wearable input mapping. Docker Desktop was accessed through its Windows executable. The image build,
Compose validation and isolated Python 3.13/PostgreSQL 15 checks passed with
283 tests and 99.90% combined application coverage. Bootstrap/migrations also
passed inside the container. Local Python 3.12/PostgreSQL
16 checks passed the same suite (99.96% lines, 99.67% branches).
