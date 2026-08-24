import { AddressLookup } from "@/components/AddressLookup";

export const dynamic = "force-dynamic";

export default async function LookupPage({ searchParams }: PageProps<"/lookup">) {
  const params = await searchParams;
  const q = typeof params.q === "string" ? params.q : "";

  return (
    <main className="flex-1 mx-auto w-full max-w-3xl px-5 sm:px-8 py-10">
      <header className="mb-8">
        <p className="eyebrow mb-3">Public lookup</p>
        <h1 className="display text-3xl sm:text-4xl text-bone">
          What&apos;s under your street?
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-bone-dim">
          Search a Houston address to see the soil beneath it, whether that soil can be
          read at all, and any water complaint the city has already triaged nearby.
        </p>
      </header>

      <AddressLookup initialQuery={q} />
    </main>
  );
}
