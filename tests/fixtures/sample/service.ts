import { User } from "./auth";

export class Session {
  start(u: User) {
    return login(u);
  }
}

function login(u: User) {
  return true;
}
