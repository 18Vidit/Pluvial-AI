import { AddressConsole } from "@/components/AddressConsole";

export const metadata = {
  title: "Pluvial-AI — what the ground will do to this address",
  description:
    "Enter any US address. Real ground data is fetched live from Mireye and three pairs of agents argue over it, with every claim anchored to a sampled point on the map.",
};

export default function AddressPage() {
  return <AddressConsole />;
}
