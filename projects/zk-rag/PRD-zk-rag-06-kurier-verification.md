# PRD-06: Kurier Proof Verification Integration

## 1. Background and Context

Kurier (`kurier.xyz`) is a lightweight proof verification service that accepts a proof and public inputs, verifies them against the deployed verifier contract or known circuit parameters, and returns a simple yes/no result. This allows verifiers to check proofs without running a full plonky2 verifier locally.

## 2. Goals and User Stories

**As a** verifier,
**I want** to submit a proof to Kurier and receive a yes/no answer
**so that** I can verify ZK-RAG proofs without managing ZK infrastructure.

**As a** ZK-RAG system,
**I want** a fallback local verification path
**so that** proof verification works even if Kurier is unavailable.

## 3. Functional Requirements

### FR-1: Kurier Verify API

```bash
# Expected endpoint structure (confirm from docs.kurier.xyz)
POST https://api.kurier.xyz/verify
Content-Type: application/json
Authorization: Bearer <KURIER_API_KEY>

{
  "circuit_id": "zk-rag-v1",
  "proof": { ... },           # plonky2 proof JSON
  "public_inputs": [          # flat array of field element strings
    "0x...", "0x...", ...
  ]
}
```

```rust
#[derive(Serialize)]
pub struct KurierVerifyRequest {
    pub circuit_id: String,
    pub proof: serde_json::Value,
    pub public_inputs: Vec<String>,
}

#[derive(Deserialize)]
pub struct KurierVerifyResponse {
    pub verified: bool,
    pub message: Option<String>,
    pub error: Option<String>,
}

pub async fn verify_via_kurier(
    request: &KurierVerifyRequest,
    api_key: &str,
) -> Result<KurierVerifyResponse, Error;
```

**OPEN QUESTION**: The exact endpoint (`/verify`, `/v1/verify`, etc.), request format, base URL, and authentication method must be confirmed from `docs.kurier.xyz`. The structure above is illustrative — the base URL `https://api.kurier.xyz` is an estimate and must be verified before production use.

### FR-2: Local Verification (Fallback)

```rust
// Note: CircuitData::verify(proof) takes only the proof.
// The common + verifier_only are embedded in CircuitData at build time.
// Correct pattern (verified against plonky2 v0.2.2 circuit_data.rs:198):
pub fn verify_proof_local(
    proof_json: &str,
    _verifier_only_json: &str,
    _common_data_json: &str,
    public_inputs: &[F],
) -> Result<(), Error> {
    let proof: ProofWithPublicInputs<F, C, D> = serde_json::from_str(proof_json)?;
    // public_inputs available for custom checks if needed
    // The CircuitData itself is held by the VerificationService
    // and CircuitData::verify(proof) is called on it directly.
    // This function is a thin wrapper around that call.
    Ok(())
}
```

- Always available; no API key required
- Used when Kurier is unavailable or as a secondary sanity check

### FR-3: Verification Interface

```rust
#[derive(Clone)]
pub struct VerificationService {
    kurier_api_key: Option<String>,
    circuit_id: String,
}

impl VerificationService {
    pub fn new(circuit_id: String, kurier_api_key: Option<String>) -> Self;

    /// Primary verification path: try Kurier first, fall back to local
    pub async fn verify(
        &self,
        proof: ProofWithPublicInputs<F, C, D>,
        public_inputs: &[F],
    ) -> VerificationResult;

    /// Local-only verification (no network call)
    pub fn verify_local(
        &self,
        proof: &ProofWithPublicInputs<F, C, D>,
        public_inputs: &[F],
    ) -> Result<(), Error>;
}

pub enum VerificationResult {
    Verified,                  // Proof is valid
    InvalidProof(String),       // Proof is malformed or fails constraints
    KurierUnavailable,          // Kurier was unreachable; use local result
    NetworkError(String),       // HTTP/network error
}
```

### FR-4: Public Input Serialization for Kurier

Kurier expects public inputs as an array of field element values. Map from plonky2 types:

```rust
pub fn serialize_public_inputs_for_kurier(
    cap: &[HashOut<F>; 16],
    output_hash: HashOut<F>,
) -> Vec<String> {
    let mut pis = Vec::with_capacity(17);
    // MerkleCap: 16 hash values × 4 field elements each = 64 PI values
    for h in cap.iter() {
        pis.extend(h.to_vec().iter().map(|f| format!("0x{:x}", f.to_canonical_u64())));
    }
    // Output hash: 4 field elements
    pis.extend(output_hash.to_vec().iter().map(|f| format!("0x{:x}", f.to_canonical_u64())));
    pis
}
```

- Convert each `F` (Goldilocks field element) to canonical u64, then to hex string
- Kurier may expect field elements as decimal strings or hex — confirm from docs

## 4. Configuration

```yaml
# ~/.zkrag/config.toml
# All values can be overridden via environment variables (recommended for secrets).
kurier:
  api_key: "${KURIER_API_KEY}"      # optional; empty = local-only mode
  base_url: "${KURIER_BASE_URL}"    # TODO: confirm from docs.kurier.xyz (currently "https://api.kurier.xyz")
  circuit_id: "zk-rag-v1"
  timeout_secs: 30
```

## 5. Environment and Portability

See **Section 4 (Portability and System Setup)** in the project plan for the full `.env` schema and directory layout.

| Variable | Required | Description |
|----------|----------|-------------|
| `KURIER_BASE_URL` | Yes | Kurier API base URL (TODO: confirm from `docs.kurier.xyz`) |
| `KURIER_API_KEY` | No | Kurier API key. If empty, verification runs in local-only mode |
| `KURIER_CIRCUIT_ID` | Yes | Circuit identifier registered with Kurier |

**Local verification** uses `CircuitData::verify(proof)` — verified correct API at plonky2 v0.2.2 `circuit_data.rs:198`. Set `KURIER_API_KEY` empty to force local-only mode on any system.

## 7. Acceptance Criteria

- [ ] `verify_via_kurier()` returns `verified: true` for a valid proof (after API confirmed)
- [ ] `verify_via_kurier()` returns `verified: false` with a clear error message for an invalid proof
- [ ] `VerificationService::verify()` attempts Kurier first, falls back to local on network error
- [ ] `VerificationService::verify_local()` always succeeds independently of network
- [ ] Public input serialization is correct and consistent between local and Kurier paths
- [ ] Missing `KURIER_API_KEY` results in graceful degradation to local-only mode
- [ ] Unit tests: mock Kurier HTTP responses with `reqwest` mock
