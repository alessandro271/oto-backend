// Le bac à sable CONVERSATIONNEL (#1202) : le même Pyodide sous Deno que `sandbox.ts`,
// avec les mêmes permissions (LECTURE de Pyodide et des roues, rien d'autre), mais le
// code peut demander des appels d'outils au serveur pendant qu'il tourne.
//
// Protocole, une ligne JSON par message :
//   entrée  1re ligne : la tâche {pyodide, wheels, nonce, harness, job: {sources,
//           entrypoint, input}} ; ensuite, les réponses du serveur {id, ok, result |
//           code, message}.
//   sortie  chaque ligne préfixée par `nonce` : {type: "call", id, tool, args} pour un
//           appel, puis {type: "done", ok, sortie | erreur, journal} pour finir.
// Le nonce ne vit que dans cette fermeture, hors de `globalThis` : le code Python peut
// écrire sur la sortie standard (par `js.Deno`), il ne peut pas y forger un appel ni une
// fin. Il peut LIRE l'entrée standard — il ne volerait que ses propres réponses.
//
// Aucun import au-delà de Pyodide : l'exécuteur lance Deno en `--cached-only --frozen`,
// d'où le lecteur de lignes écrit à la main.
import { loadPyodide } from "npm:pyodide@0.26.4";

const encodeur = new TextEncoder();
const decodeur = new TextDecoder();
const lecteur = Deno.stdin.readable.getReader();
let tampon = "";

async function ligne(): Promise<string | null> {
  while (true) {
    const i = tampon.indexOf("\n");
    if (i >= 0) {
      const l = tampon.slice(0, i);
      tampon = tampon.slice(i + 1);
      return l;
    }
    const { value, done } = await lecteur.read();
    if (done) return null;
    tampon += decodeur.decode(value, { stream: true });
  }
}

const premiere = await ligne();
const tache = JSON.parse(premiere ?? "{}");
const nonce: string = tache.nonce;
const ecrire = (objet: unknown) => {
  const octets = encodeur.encode(nonce + JSON.stringify(objet) + "\n");
  let fait = 0;
  while (fait < octets.length) fait += Deno.stdout.writeSync(octets.subarray(fait));
};

let journal = "";
const MAX_JOURNAL = 20_000;
const noter = (l: string) => {
  if (journal.length < MAX_JOURNAL) journal += l + "\n";
};

// Les appels en attente de leur réponse, par numéro. Une seule boucle lit l'entrée.
const attente = new Map<number, (brut: string) => void>();
let prochain = 0;
(async () => {
  while (true) {
    const l = await ligne();
    if (l === null) {
      for (const [, resoudre] of attente) {
        resoudre(JSON.stringify({ ok: false, code: "bridge_closed", message: "" }));
      }
      attente.clear();
      return;
    }
    const message = JSON.parse(l);
    const resoudre = attente.get(message.id);
    if (resoudre) {
      attente.delete(message.id);
      resoudre(l);
    }
  }
})();

const appelOutil = (outil: string, argumentsJson: string) =>
  new Promise<string>((resoudre) => {
    const id = ++prochain;
    attente.set(id, resoudre);
    ecrire({ type: "call", id, tool: outil, args: JSON.parse(argumentsJson) });
  });

try {
  const py = await loadPyodide({
    indexURL: tache.pyodide + "/",
    stdout: noter,
    stderr: noter,
  });
  for (const roue of tache.wheels) {
    py.unpackArchive(Deno.readFileSync(roue), "wheel");
  }
  py.globals.set("_tache_json", JSON.stringify(tache.job));
  py.globals.set("_appel_outil", appelOutil);
  const sortie = await py.runPythonAsync(tache.harness);
  ecrire({ type: "done", ok: true, sortie: JSON.parse(sortie), journal });
} catch (e) {
  ecrire({ type: "done", ok: false, erreur: String(e).slice(0, 4000), journal });
}
// La boucle de lecture garde une lecture en attente : sans sortie explicite, le
// processus attendrait l'entrée standard indéfiniment.
Deno.exit(0);
