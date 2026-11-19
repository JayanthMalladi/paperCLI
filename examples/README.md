# examples/ — Phase 8 pilot artifacts

- `brief-robotics.yaml` — the Phase-8 pilot research brief (user-supplied
  robotics question, dashes cleaned). Valid against the published brief
  schema (`papers schema --validate --schema-name brief --input ...`).
- `review-plan-robotics.yaml` — the validated protocol + planned Run row,
  produced by
  `papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml`.
  Contains the protocol hash, plan id, resource estimate, and the 8 guardrail
  limits (spec section 7).
- `doctor-preflight-record.json` — the preflight record from
  `papers doctor --json` on this project (machine-local paths scrubbed;
  secret values are absent — provider/model credentials appear only as
  env-var reference names, never values).
- `sample-brief.yaml` — earlier sample brief from the build (spec section 7).

Launch the pilot (Phase 8, one command per step — see README "Launch the
overnight pilot"):

1. `papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml`
2. `papers review run --plan examples/review-plan-robotics.yaml --detach`
3. `papers review status <RUN_ID>` / `papers review resume <RUN_ID> --detach`
4. `papers review export <RUN_ID> --formats md,pdf,docx,bib,csl,csv`
