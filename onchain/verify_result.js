#!/usr/bin/env node
/**
 * Verify a fixture's FINAL SCORE on-chain via TxLINE Merkle proofs.
 *
 * Flow (devnet by default):
 *   1. fresh guest JWT + activated X-Api-Token (from env) -> TxLINE API
 *   2. /api/scores/snapshot/{fixtureId} -> the game_finalised record (Seq, goals)
 *   3. /api/scores/stat-validation?fixtureId&seq&statKeys=1,2 -> Merkle proof bundle
 *   4. program.validateStatV2(payload, "goals == H && goals == A") .view()
 *      -> true only if the on-chain daily Merkle root proves the final score
 *   5. optionally (--send) lands the same validation as a REAL transaction so
 *      anyone can audit it on the explorer.
 *
 * Prints a single JSON line: {valid, fixtureId, seq, home, away, txSig, explorer}
 *
 * Env: TXLINE_API_BASE, TXLINE_API_TOKEN (required)
 *      WALLET_PATH (default ../../.keys/devnet-wallet.json relative to this file)
 *      SOLANA_RPC (default https://api.devnet.solana.com)
 * Usage: node verify_result.js --fixture 18241006 [--send]
 */
const fs = require("fs");
const path = require("path");
const anchor = require("@coral-xyz/anchor");
const { PublicKey, Keypair, Connection, ComputeBudgetProgram } = require("@solana/web3.js");
const BN = require("bn.js");
const axios = require("axios");

const IDL = require("./txoracle.devnet.json");

function arg(name) {
  const i = process.argv.indexOf(name);
  return i === -1 ? null : (process.argv[i + 1] ?? true);
}

function toBytes32(value) {
  const bytes = Array.isArray(value) ? Uint8Array.from(value)
    : value instanceof Uint8Array ? value
    : typeof value === "string" && value.startsWith("0x") ? Buffer.from(value.slice(2), "hex")
    : Buffer.from(value, "base64");
  if (bytes.length !== 32) throw new Error(`expected 32 bytes, got ${bytes.length}`);
  return Array.from(bytes);
}

const toProofNodes = (nodes) => nodes.map((n) => ({
  hash: toBytes32(n.hash),
  isRightSibling: n.isRightSibling,
}));

async function main() {
  const fixtureId = parseInt(arg("--fixture"), 10);
  if (!fixtureId) throw new Error("--fixture <id> is required");
  const send = process.argv.includes("--send");

  const apiBase = (process.env.TXLINE_API_BASE || "https://txline-dev.txodds.com").replace(/\/$/, "");
  const apiToken = process.env.TXLINE_API_TOKEN;
  if (!apiToken) throw new Error("TXLINE_API_TOKEN not set");

  const { data: guest } = await axios.post(`${apiBase}/auth/guest/start`, {});
  const http = axios.create({
    baseURL: apiBase,
    timeout: 30000,
    headers: { Authorization: `Bearer ${guest.token}`, "X-Api-Token": apiToken },
  });

  // --- final record: game_finalised sets StatusId/period 100 --------------
  const { data: snapshot } = await http.get(`/api/scores/snapshot/${fixtureId}`);
  const items = Array.isArray(snapshot) ? snapshot : snapshot.items || [];
  let finalRec = null;
  for (const it of items) {
    const action = String(it.Action || it.action || "").toLowerCase();
    const status = parseInt(it.StatusId ?? it.statusId ?? 0, 10);
    if (action === "game_finalised" || status === 100) {
      if (!finalRec || (it.Seq || 0) > (finalRec.Seq || 0)) finalRec = it;
    }
  }
  if (!finalRec) throw new Error("no game_finalised record yet — match not finished");
  const seq = finalRec.Seq ?? finalRec.seq;
  const score = finalRec.Score || {};
  const goals = (p) => ((score[p] || {}).Total || {}).Goals || 0;
  const home = goals("Participant1");
  const away = goals("Participant2");

  // --- Merkle proof bundle for [P1 total goals, P2 total goals] ------------
  const { data: val } = await http.get("/api/scores/stat-validation", {
    params: { fixtureId, seq, statKeys: "1,2" },
  });

  const payload = {
    ts: new BN(val.summary.updateStats.minTimestamp),
    fixtureSummary: {
      fixtureId: new BN(val.summary.fixtureId),
      updateStats: {
        updateCount: val.summary.updateStats.updateCount,
        minTimestamp: new BN(val.summary.updateStats.minTimestamp),
        maxTimestamp: new BN(val.summary.updateStats.maxTimestamp),
      },
      eventsSubTreeRoot: toBytes32(val.summary.eventStatsSubTreeRoot),
    },
    fixtureProof: toProofNodes(val.subTreeProof),
    mainTreeProof: toProofNodes(val.mainTreeProof),
    eventStatRoot: toBytes32(val.eventStatRoot),
    stats: val.statsToProve.map((statObj, i) => ({
      stat: statObj,
      statProof: toProofNodes(val.statProofs[i]),
    })),
  };

  // strategy: P1 goals == home AND P2 goals == away (indexes follow statKeys)
  const strategy = {
    geometricTargets: [],
    distancePredicate: null,
    discretePredicates: [
      { single: { index: 0, predicate: { threshold: home, comparison: { equalTo: {} } } } },
      { single: { index: 1, predicate: { threshold: away, comparison: { equalTo: {} } } } },
    ],
  };

  // --- anchor: devnet program + daily_scores_roots PDA ---------------------
  const walletPath = process.env.WALLET_PATH
    || path.join(__dirname, "..", "..", ".keys", "devnet-wallet.json");
  const keypair = Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(walletPath, "utf8"))));
  const connection = new Connection(
    process.env.SOLANA_RPC || "https://api.devnet.solana.com", "confirmed");
  const provider = new anchor.AnchorProvider(
    connection, new anchor.Wallet(keypair), anchor.AnchorProvider.defaultOptions());
  const program = new anchor.Program(IDL, provider);

  const epochDay = Math.floor(val.summary.updateStats.minTimestamp / 86400000);
  const [dailyScoresPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("daily_scores_roots"), new BN(epochDay).toArrayLike(Buffer, "le", 2)],
    program.programId);

  const computeBudgetIx = ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 });

  const valid = await program.methods
    .validateStatV2(payload, strategy)
    .accounts({ dailyScoresMerkleRoots: dailyScoresPda })
    .preInstructions([computeBudgetIx])
    .view();

  let txSig = null;
  if (send && valid) {
    // land the proof as a real tx so the result is auditable on the explorer
    txSig = await program.methods
      .validateStatV2(payload, strategy)
      .accounts({ dailyScoresMerkleRoots: dailyScoresPda })
      .preInstructions([computeBudgetIx])
      .rpc();
  }

  console.log(JSON.stringify({
    valid, fixtureId, seq, home, away, txSig,
    explorer: txSig
      ? `https://explorer.solana.com/tx/${txSig}?cluster=devnet`
      : null,
    program: program.programId.toBase58(),
  }));
}

main().catch((err) => {
  console.log(JSON.stringify({ valid: false, error: String(err.message || err) }));
  process.exit(1);
});
