/** Address mode is the one page shaped like an app screen rather than a
 *  document: a map and three argument lanes meant to fill exactly one
 *  viewport, each scrolling internally, with the chat composer always
 *  reachable below them.
 *
 *  The root layout only sets `min-h-full` on body, which lets ordinary
 *  pages (the landing page, the board, a case file) grow past one screen
 *  and let the document scroll — correct for long-form content. That same
 *  choice means nothing constrains AddressConsole's height, so its inner
 *  `min-h-0`/`flex-1`/`overflow-y-auto` scroll regions have no bounded
 *  ancestor to size against: the lanes never hit a height limit, so they
 *  never scroll, and the whole page grows to fit every claim streamed
 *  in — five thousand pixels tall on a real run, with the chat composer
 *  pushed off screen at the bottom.
 *
 *  This route-scoped layout gives just this page an explicit height —
 *  the viewport minus the 57px header — so `min-h-0` has something real
 *  to shrink against, without touching the root layout every other page
 *  depends on for natural scroll.
 */
export default function AddressLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-[calc(100dvh-57px)] flex-col overflow-hidden">
      {children}
    </div>
  );
}
