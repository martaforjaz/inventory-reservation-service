using System.Diagnostics;
using System.Diagnostics.Metrics;
using Npgsql;

var builder = WebApplication.CreateBuilder(args);
var connection = builder.Configuration.GetConnectionString("Inventory")
    ?? throw new InvalidOperationException("Inventory connection string is required.");
builder.Services.AddSingleton(_ => NpgsqlDataSource.Create(connection));
builder.Services.AddHealthChecks();
var app = builder.Build();
var meter = new Meter("Inventory.Reservations");
var requests = meter.CreateCounter<long>("inventory.requests");
var latency = meter.CreateHistogram<double>("inventory.request.duration", "ms");
app.Use(async (context, next) =>
{
    var start = Stopwatch.GetTimestamp();
    await next(context);
    var status = new KeyValuePair<string, object?>("status", context.Response.StatusCode);
    requests.Add(1, status);
    latency.Record(Stopwatch.GetElapsedTime(start).TotalMilliseconds, status);
});
app.MapHealthChecks("/health/live");
app.MapGet("/health/ready", async (NpgsqlDataSource db, CancellationToken ct) =>
{
    await using var query = db.CreateCommand("SELECT 1 FROM inventory LIMIT 1");
    await query.ExecuteScalarAsync(ct);
    return Results.Ok();
});
app.MapGet("/stock/{sku}", async (string sku, NpgsqlDataSource db, CancellationToken ct) =>
{
    await using var query = db.CreateCommand("SELECT available FROM inventory WHERE sku=$1");
    query.Parameters.AddWithValue(sku);
    return await query.ExecuteScalarAsync(ct) is int remaining
        ? Results.Ok(new { sku, remaining }) : Results.NotFound();
});

app.MapGet("/reservations/{id:guid}", async (Guid id, NpgsqlDataSource db, CancellationToken ct) =>
{
    await using var query = db.CreateCommand("SELECT sku, quantity, cancelled FROM reservations WHERE id=$1");
    query.Parameters.AddWithValue(id);
    await using var row = await query.ExecuteReaderAsync(ct);
    return await row.ReadAsync(ct)
        ? Results.Ok(new { id, sku = row.GetString(0), quantity = row.GetInt32(1), cancelled = row.GetBoolean(2) })
        : Results.NotFound();
});

app.MapPost("/reservations/{id:guid}", async (Guid id, Reservation request, NpgsqlDataSource db, CancellationToken ct) =>
{
    if (id == Guid.Empty || string.IsNullOrWhiteSpace(request.Sku) || request.Quantity < 1)
        return Results.BadRequest();
    await using var conn = await db.OpenConnectionAsync(ct);
    await using var tx = await conn.BeginTransactionAsync(ct);
    await using var insert = Command(conn, tx,
        "INSERT INTO reservations(id,sku,quantity) VALUES ($1,$2,$3) ON CONFLICT (id) DO NOTHING RETURNING id",
        id, request.Sku, request.Quantity);
    if (await insert.ExecuteScalarAsync(ct) is null)
    {
        await using var existing = Command(conn, tx, "SELECT sku,quantity,cancelled FROM reservations WHERE id=$1", id);
        await using var row = await existing.ExecuteReaderAsync(ct);
        await row.ReadAsync(ct);
        return row.GetString(0) == request.Sku && row.GetInt32(1) == request.Quantity
            ? Results.Ok(new { id, cancelled = row.GetBoolean(2) })
            : Results.Conflict(new { error = "Reservation ID already used with different values." });
    }
    await using var debit = Command(conn, tx,
        "UPDATE inventory SET available=available-$2 WHERE sku=$1 AND available >= $2",
        request.Sku, request.Quantity);
    if (await debit.ExecuteNonQueryAsync(ct) != 1)
        return Results.Conflict(new { error = "Insufficient stock or unknown SKU." });
    await tx.CommitAsync(ct);
    return Results.Created($"/reservations/{id}", new { id, cancelled = false });
});

app.MapDelete("/reservations/{id:guid}", async (Guid id, NpgsqlDataSource db, CancellationToken ct) =>
{
    await using var conn = await db.OpenConnectionAsync(ct);
    await using var tx = await conn.BeginTransactionAsync(ct);
    await using var refund = Command(conn, tx, """
        WITH cancelled AS (
          UPDATE reservations SET cancelled=true WHERE id=$1 AND NOT cancelled RETURNING sku,quantity
        )
        UPDATE inventory AS i SET available=i.available+c.quantity FROM cancelled AS c WHERE i.sku=c.sku
        """, id);
    if (await refund.ExecuteNonQueryAsync(ct) == 0)
    {
        await using var existing = Command(conn, tx, "SELECT 1 FROM reservations WHERE id=$1", id);
        if (await existing.ExecuteScalarAsync(ct) is null) return Results.NotFound();
    }
    await tx.CommitAsync(ct);
    return Results.NoContent();
});

app.Run();

static NpgsqlCommand Command(NpgsqlConnection connection, NpgsqlTransaction tx, string sql, params object[] values)
{
    var command = new NpgsqlCommand(sql, connection, tx);
    foreach (var value in values) command.Parameters.Add(new NpgsqlParameter { Value = value });
    return command;
}

record Reservation(string Sku, int Quantity);
