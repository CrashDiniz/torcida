#!/usr/bin/env node
/**
 * TxLINE MAINNET subscription — the "finalize on mainnet" step from the
 * workshop. Free World Cup tier: price 0 TxL/week, you only pay SOL fees
 * (ATA rent + tx, ~0.005 SOL total).
 *
 * Flow: guest JWT -> ensure Token-2022 ATA -> on-chain `subscribe(level, weeks)`
 *       -> sign `${txSig}:${leagues}:${jwt}` -> POST /token/activate -> API token.
 *
 * Prints JSON: {txSig, apiToken, jwt, wallet, explorer} — put apiToken in .env
 * as TXLINE_API_TOKEN with TXLINE_API_BASE=https://txline.txodds.com.
 *
 * Usage: WALLET_PATH=../../.keys/mainnet-wallet.json node subscribe_mainnet.js [--level 1] [--weeks 4]
 */
const fs = require("fs");
const path = require("path");
const anchor = require("@coral-xyz/anchor");
const { PublicKey, Keypair, Connection, Transaction, SystemProgram,
        sendAndConfirmTransaction } = require("@solana/web3.js");
const { TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID,
        getAssociatedTokenAddressSync, getAccount,
        createAssociatedTokenAccountInstruction } = require("@solana/spl-token");
const nacl = require("tweetnacl");
const axios = require("axios");

const IDL = require("./txoracle.mainnet.json");
const API = "https://txline.txodds.com";
const MINT = new PublicKey("Zhw9TVKp68a1QrftncMSd6ELXKDtpVMNuMGr1jNwdeL");

function arg(name, dflt) {
  const i = process.argv.indexOf(name);
  return i === -1 ? dflt : parseInt(process.argv[i + 1], 10);
}

async function main() {
  const level = arg("--level", 1);   // mainnet SL1: World Cup free tier (60s delay)
  const weeks = arg("--weeks", 4);   // must be a multiple of 4
  const leagues = [];                // default bundle

  const walletPath = process.env.WALLET_PATH
    || path.join(__dirname, "..", "..", ".keys", "mainnet-wallet.json");
  const user = Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(walletPath, "utf8"))));
  const connection = new Connection(
    process.env.SOLANA_RPC || "https://api.mainnet-beta.solana.com", "confirmed");

  const balance = await connection.getBalance(user.publicKey);
  console.error(`wallet ${user.publicKey.toBase58()} balance ${balance / 1e9} SOL`);
  if (balance < 6_000_000) {
    throw new Error("fund the wallet with ~0.02 SOL first");
  }

  const { data: guest } = await axios.post(`${API}/auth/guest/start`);
  const jwt = guest.token;

  const provider = new anchor.AnchorProvider(
    connection, new anchor.Wallet(user), anchor.AnchorProvider.defaultOptions());
  const program = new anchor.Program(IDL, provider);

  const ata = getAssociatedTokenAddressSync(MINT, user.publicKey, false,
                                            TOKEN_2022_PROGRAM_ID);
  if (!(await connection.getAccountInfo(ata))) {
    console.error("creating Token-2022 ATA…");
    const tx = new Transaction().add(createAssociatedTokenAccountInstruction(
      user.publicKey, ata, user.publicKey, MINT,
      TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID));
    await sendAndConfirmTransaction(connection, tx, [user],
                                    { commitment: "confirmed" });
    await new Promise(r => setTimeout(r, 3000));
  }
  const tokenAccount = await getAccount(connection, ata, "confirmed",
                                        TOKEN_2022_PROGRAM_ID);

  const [pricingMatrix] = PublicKey.findProgramAddressSync(
    [Buffer.from("pricing_matrix")], program.programId);
  const [treasuryPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("token_treasury_v2")], program.programId);
  const treasuryVault = getAssociatedTokenAddressSync(MINT, treasuryPda, true,
                                                      TOKEN_2022_PROGRAM_ID);

  console.error(`subscribing on-chain: level ${level}, ${weeks} weeks…`);
  const tx = await program.methods
    .subscribe(level, weeks)
    .accounts({
      user: user.publicKey,
      pricingMatrix,
      tokenMint: MINT,
      userTokenAccount: tokenAccount.address,
      tokenTreasuryVault: treasuryVault,
      tokenTreasuryPda: treasuryPda,
      tokenProgram: TOKEN_2022_PROGRAM_ID,
      associatedTokenProgram: ASSOCIATED_TOKEN_PROGRAM_ID,
      systemProgram: SystemProgram.programId,
    })
    .transaction();
  const bh = await connection.getLatestBlockhash("confirmed");
  tx.recentBlockhash = bh.blockhash;
  tx.feePayer = user.publicKey;
  tx.sign(user);
  const txSig = await connection.sendRawTransaction(tx.serialize());
  await connection.confirmTransaction(
    { signature: txSig, blockhash: bh.blockhash,
      lastValidBlockHeight: bh.lastValidBlockHeight }, "confirmed");
  console.error(`subscribe tx confirmed: ${txSig}`);

  const message = new TextEncoder().encode(`${txSig}:${leagues.join(",")}:${jwt}`);
  const walletSignature = Buffer.from(
    nacl.sign.detached(message, user.secretKey)).toString("base64");
  const { data: activation } = await axios.post(
    `${API}/api/token/activate`,
    { txSig, walletSignature, leagues },
    { headers: { Authorization: `Bearer ${jwt}` } });

  console.log(JSON.stringify({
    txSig,
    apiToken: activation.token || activation,
    jwt,
    wallet: user.publicKey.toBase58(),
    explorer: `https://explorer.solana.com/tx/${txSig}`,
  }));
}

main().catch((err) => {
  console.error(err.response?.data || err);
  process.exit(1);
});
