import Link from "next/link";
import { notFound } from "next/navigation";
import { fetchVerdict } from "@/lib/api";
import { CaseFile } from "@/components/CaseFile";

export const dynamic = "force-dynamic";

export default async function CasePage({ params }: PageProps<"/case/[id]">) {
  const { id } = await params;
  const verdictId = Number(id);
  if (!Number.isFinite(verdictId)) notFound();

  let detail;
  try {
    detail = await fetchVerdict(verdictId);
  } catch {
    notFound();
  }

  const streetName = detail.segment?.name ?? `Segment ${detail.verdict.segment_id}`;

  return (
    <main className="flex-1 mx-auto w-full max-w-7xl px-5 sm:px-8 py-8 sm:py-10">
      <nav aria-label="Breadcrumb" className="mb-6">
        <Link
          href="/board"
          className="data inline-flex items-center gap-1.5 text-xs text-bone-faint transition-colors duration-200 hover:text-bone"
        >
          ← back to queue
        </Link>
      </nav>

      <header className="mb-9">
        <p className="eyebrow mb-3">
          Case file · verdict {detail.verdict.verdict_id} · agent {detail.verdict.agent_version}
        </p>
        <h1 className="display text-3xl sm:text-5xl text-bone">{streetName}</h1>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-bone-dim">
          One complaint, four agents, and the ground underneath. Everything below is the
          record as written — including the evidence that was considered and set aside.
        </p>
      </header>

      <CaseFile detail={detail} />
    </main>
  );
}
