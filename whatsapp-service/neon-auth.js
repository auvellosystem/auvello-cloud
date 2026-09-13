import pg from "pg";
import { BufferJSON, initAuthCreds, proto } from "@whiskeysockets/baileys";

const { Pool } = pg;

/**
 * Estado de autenticacao do Baileys persistido no PostgreSQL/Neon.
 *
 * Formato inspirado no useMultiFileAuthState, mas em vez de gravar arquivos
 * dentro de auth_info, cada chave e salva em whatsapp_auth no Neon.
 */
export async function useNeonAuthState(databaseUrl) {
  if (!databaseUrl) {
    throw new Error("DATABASE_URL nao informada para a sessao do WhatsApp.");
  }

  const pool = new Pool({
    connectionString: databaseUrl,
    max: 3,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 15_000
  });

  await pool.query(`
    CREATE TABLE IF NOT EXISTS whatsapp_auth (
      auth_key TEXT PRIMARY KEY,
      auth_value JSONB NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);

  const serialize = (value) => JSON.stringify(value, BufferJSON.replacer);
  const deserialize = (value) => {
    if (value == null) return null;
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return JSON.parse(text, BufferJSON.reviver);
  };

  async function readData(key) {
    const result = await pool.query(
      "SELECT auth_value FROM whatsapp_auth WHERE auth_key = $1 LIMIT 1",
      [key]
    );
    if (!result.rowCount) return null;
    return deserialize(result.rows[0].auth_value);
  }

  async function writeData(key, value) {
    const serialized = serialize(value);
    await pool.query(
      `
      INSERT INTO whatsapp_auth (auth_key, auth_value, updated_at)
      VALUES ($1, $2::jsonb, NOW())
      ON CONFLICT (auth_key)
      DO UPDATE SET auth_value = EXCLUDED.auth_value, updated_at = NOW()
      `,
      [key, serialized]
    );
  }

  async function removeData(key) {
    await pool.query("DELETE FROM whatsapp_auth WHERE auth_key = $1", [key]);
  }

  let creds = await readData("creds");
  if (!creds) {
    creds = initAuthCreds();
  }

  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          await Promise.all(
            ids.map(async (id) => {
              let value = await readData(`${type}-${id}`);
              if (type === "app-state-sync-key" && value) {
                value = proto.Message.AppStateSyncKeyData.fromObject(value);
              }
              data[id] = value;
            })
          );
          return data;
        },
        set: async (data) => {
          const tasks = [];
          for (const category of Object.keys(data)) {
            for (const id of Object.keys(data[category] || {})) {
              const value = data[category][id];
              const key = `${category}-${id}`;
              tasks.push(value ? writeData(key, value) : removeData(key));
            }
          }
          await Promise.all(tasks);
        }
      }
    },
    saveCreds: async () => {
      await writeData("creds", creds);
    },
    close: async () => {
      await pool.end();
    }
  };
}
