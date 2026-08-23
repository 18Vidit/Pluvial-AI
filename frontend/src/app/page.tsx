import { fetchQueue } from "@/lib/api";
import { VerdictCard } from "@/components/VerdictCard";
import { Disposition, QueueCard } from "@/lib/types";

const COLUMNS: { key: Disposition[]; title: string; hint: string }[] = [
  { key: ["dispatch"], title: "Dispatch now", hint: "Crew should go today" },
  { key: ["inspect"], title: "Inspect this week", hint: "Worth a camera/manual check" },
  { key: ["monitor", "close"], title: "Monitor / Close", hint: "No action, but tracked" },
];

function groupByColumn(cards: QueueCard[]) {
  return COLUMNS.map((col) => ({
    ...col,
    cards: cards.filter((c) => col.key.includes(c.disposition)),
  }));
}

export default async function DispatcherBoard() {
  let cards: QueueCard[] = [];
  let error: string | null = null;
  try {
    const data = await fetchQueue();
    cards = data.cards;
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  const columns = groupByColumn(cards);

  return (
    <main className="flex-1 p-6 max-w-7xl mx-auto w-full">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Bellwether</h1>
        <p className="text-neutral-500 text-sm mt-1">
          Houston 311 water complaints, triaged against soil movement potential and the current moisture trigger state.
        </p>
      </header>

      {error && (
        <div className="mb-6 p-4 rounded-lg bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300 text-sm">
          Could not reach the Bellwether API ({error}). Is the backend running on port 8811?
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {columns.map((col) => (
          <div key={col.title}>
            <div className="mb-3">
              <h2 className="font-semibold">{col.title}</h2>
              <p className="text-xs text-neutral-400">{col.hint}</p>
            </div>
            <div className="space-y-3">
              {col.cards.length === 0 && (
                <p className="text-sm text-neutral-400 italic">No cases</p>
              )}
              {col.cards.map((card) => (
                <VerdictCard key={card.verdict_id} card={card} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </main>
  );
}
