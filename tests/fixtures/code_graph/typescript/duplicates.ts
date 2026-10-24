import { charge, ping } from './gateway';

export class Primary {
  retry() { return charge(); }
}

export class Secondary {
  retry() { return ping(); }
}
