import { fetchQueue } from "@/lib/api";
import { QueueBoard } from "@/components/QueueBoard";
import { QueueCard } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function BoardPage() {
  let cards: QueueCard[] = [];
  let error: string | null = null;

  try {
    cards = (await fetchQueue()).cards;
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  return (
    <main className="flex-1 mx-auto w-full max-w-7xl px-5 sm:px-8 py-10">
      <header className="mb-8">
        <p className="eyebrow mb-3">Dispatch queue · latest verdict per street</p>
        <h1 className="display text-3xl sm:text-4xl text-bone">
          What a crew should do today
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-bone-dim">
          One card per street, carrying the disposition the Adjudicator settled on. Open a
          card to replay the argument that produced it, evidence by evidence.
        </p>
      </header>

      {error ? (
        <div className="rounded border border-oxide/40 bg-oxide/10 px-4 py-3 text-sm text-oxide-bright">
          Can&apos;t reach the Pluvial-AI API ({error}). Start it with{" "}
          <code className="data">uv run python -m pluvial.cli serve</code>.
        </div>
      ) : (
        <QueueBoard cards={cards} />
      )}
    </main>
  );
}
