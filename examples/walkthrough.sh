#!/usr/bin/env bash
# The demo journey over plain HTTP against a running `pbd-spmis serve all` (or docker compose).
# Requires curl and jq. Uses fictional data only.
set -euo pipefail
BASE=${BASE:-http://localhost:8000}
PROGRAM=cash_assistance

tok() { pbd-spmis token "$@"; }
OFFICER=$(tok --sub officer-1 --role REGISTRATION_OFFICER --programs $PROGRAM)
WORKER=$(tok --sub cw-1 --role CASE_WORKER --programs $PROGRAM)
PAYSVC=$(tok --sub svc:payments-batch --role PAYMENT_SERVICE --programs '*' --service)
AUDITOR=$(tok --sub aud-1 --role AUDITOR)

hdr() { # token purpose -> fills the H array with the privacy headers
  H=(-H "Authorization: Bearer $1" -H "X-Purpose: $2" -H "X-Program: $PROGRAM" -H "Content-Type: application/json")
}

echo "## 1. identity proofing -> person token"
hdr "$OFFICER" identity_proofing
PERSON=$(curl -sf $BASE/vault/v1/identities "${H[@]}" \
  -d '{"national_id":"NID-1001","name":"Amina Example","contact":"+100000001"}' | jq -r .person_token)
echo "person_token=$PERSON"

echo "## 2. registration (registry never sees the name or id)"
hdr "$OFFICER" registration
curl -sf $BASE/registry/v1/persons "${H[@]}" \
  -d "{\"person_token\":\"$PERSON\",\"income\":180,\"household_size\":4,\"disability_status\":\"not_certified\",\"address\":{\"street\":\"12 Market Lane\",\"district\":\"North\",\"region\":\"Northern\"}}" | jq .

echo "## 3. eligibility verification (query, do not copy)"
hdr "$WORKER" eligibility_verification
DET=$(curl -sf $BASE/eligibility/v1/eligibility/verify "${H[@]}" -d "{\"person_token\":\"$PERSON\"}")
echo "$DET" | jq .
DET_ID=$(echo "$DET" | jq -r .determination_id)

echo "## 4. what the case worker sees"
hdr "$WORKER" eligibility_verification
curl -sfi "$BASE/registry/v1/persons/$PERSON?attributes=income,address,household_size" "${H[@]}" | sed -n '1p;/^x-/Ip;/^{/p' | jq -R 'fromjson? // .'

echo "## 5. enrollment -> program-specific id and entitlement"
hdr "$WORKER" enrollment
ENR=$(curl -sf $BASE/program/v1/enrollments "${H[@]}" -d "{\"person_token\":\"$PERSON\",\"determination_id\":\"$DET_ID\"}")
echo "$ENR" | jq .
PROG_ID=$(echo "$ENR" | jq -r .program_person_id)

echo "## 6. payment instrument -> token; instruction; execution"
hdr "$OFFICER" registration
curl -sf $BASE/payments/v1/instruments "${H[@]}" -d "{\"person_token\":\"$PERSON\",\"account_number\":\"12345678\",\"bank_code\":\"BNK1\"}" | jq .
hdr "$PAYSVC" payment_execution
INS=$(curl -sf $BASE/payments/v1/instructions "${H[@]}" -d "{\"program_person_id\":\"$PROG_ID\"}" | jq -r .instruction_id)
hdr "$PAYSVC" payment_execution
curl -sf -X POST $BASE/payments/v1/instructions/$INS/execute "${H[@]}" | jq .
hdr "$WORKER" payment_status_inquiry
curl -sf $BASE/payments/v1/instructions/$INS "${H[@]}" | jq .attributes

echo "## 7. audit chain and dashboard"
curl -sf $BASE/audit/v1/chain/verify -H "Authorization: Bearer $AUDITOR" | jq .
curl -sf $BASE/audit/v1/metrics -H "Authorization: Bearer $AUDITOR" | jq .
