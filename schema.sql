CREATE TABLE inventory(sku text PRIMARY KEY, available integer NOT NULL CHECK(available >= 0));
CREATE TABLE reservations(id uuid PRIMARY KEY, sku text NOT NULL,
  quantity integer NOT NULL CHECK(quantity > 0), cancelled boolean NOT NULL DEFAULT false);
INSERT INTO inventory VALUES ('LAST',1),('LOAD',5000);
