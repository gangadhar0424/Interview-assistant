import { redirect } from "next/navigation";

// The real landing/dashboard arrives in a later phase; for now send visitors to login
// (the login page forwards already-authenticated users to /home).
export default function Home() {
  redirect("/login");
}
