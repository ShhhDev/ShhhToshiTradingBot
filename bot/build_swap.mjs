// Reads one JSON object on stdin, writes one JSON object on stdout.
//   {mode:"quote"|"build", offer:"ton"|<jetton>, ask:"ton"|<jetton>, units:"<offer units>",
//    slippage:"0.01", wallet:"<sender address>" (build only)}
// Follows the STON.fi v2 docs: simulate -> dexFactory(sim.router) -> router.getSwap*TxParams.
// The private key never comes here; this only builds the unsigned message.
import { dexFactory, Client } from "@ston-fi/sdk";
import { StonApiClient } from "@ston-fi/api";

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data;
}

const isTon = (a) => String(a).toLowerCase() === "ton";

try {
  const input = JSON.parse(await readStdin());
  const api = new StonApiClient();
  const sim = await api.simulateSwap({
    offerAddress: input.offer,
    askAddress: input.ask,
    offerUnits: String(input.units),
    slippageTolerance: String(input.slippage ?? "0.01"),
  });

  const result = {
    offer_units: String(sim.offerUnits),
    ask_units: String(sim.askUnits ?? "0"),
    min_ask_units: String(sim.minAskUnits),
    price_impact: sim.priceImpact ?? null,
    router: sim.router?.address ?? null,
  };

  if (input.mode === "build") {
    const tonClient = new Client({
      endpoint: process.env.TONCENTER_URL || "https://toncenter.com/api/v2/jsonRPC",
      apiKey: process.env.TONCENTER_API_KEY || undefined,
    });
    const dex = dexFactory(sim.router);
    const router = tonClient.open(dex.Router.create(sim.router.address));
    const proxyTon = dex.pTON.create(sim.router.ptonMasterAddress);
    const base = {
      userWalletAddress: input.wallet,
      offerAmount: sim.offerUnits,
      minAskAmount: sim.minAskUnits,
      queryId: Math.floor(Math.random() * 2 ** 31),
    };

    let tx;
    if (isTon(input.offer)) {
      tx = await router.getSwapTonToJettonTxParams({ ...base, askJettonAddress: sim.askAddress, proxyTon });
    } else if (isTon(input.ask)) {
      tx = await router.getSwapJettonToTonTxParams({ ...base, offerJettonAddress: sim.offerAddress, proxyTon });
    } else {
      tx = await router.getSwapJettonToJettonTxParams({
        ...base, offerJettonAddress: sim.offerAddress, askJettonAddress: sim.askAddress,
      });
    }
    result.to = tx.to.toString();
    result.value = tx.value.toString();
    result.body = tx.body ? tx.body.toBoc().toString("base64") : null;
  }

  process.stdout.write(JSON.stringify(result));
} catch (e) {
  process.stderr.write(JSON.stringify({ error: String(e?.message ?? e).slice(0, 400) }));
  process.exit(1);
}
