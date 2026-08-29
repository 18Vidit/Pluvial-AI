/** Address mode now scrolls naturally: the map sticks to the top-left
 *  while the agent lanes and chat composer scroll past it. The old
 *  viewport-lock (`h-[calc(100dvh-57px)]` + `overflow-hidden`) cramped
 *  the three agent lanes and the chat into whatever space the map left,
 *  causing text to overlap and the chat to be unusable. Removing the
 *  lock and letting the page scroll fixes both problems while keeping
 *  the map always visible via `position: sticky` inside the console.
 */
export default function AddressLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-[calc(100dvh-57px)] flex-col">
      {children}
    </div>
  );
}
