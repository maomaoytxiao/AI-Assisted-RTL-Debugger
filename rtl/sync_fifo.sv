// sync_fifo.sv - single-clock FIFO
//
// Behavior summary (see docs/fifo_spec.md for the full specification):
//   * rst_n is active-low, asynchronous assert / synchronous release.
//     Reset clears both pointers and rd_data (empty=1, full=0, count=0).
//   * Write accepted when (wr_en && !full); writes while full are dropped.
//   * Read accepted when (rd_en && !empty); reads while empty are dropped.
//   * Read latency = 1 cycle: rd_data is registered and updates right after
//     the rising edge that accepts the read; it holds its value otherwise.
//   * full/empty/count are derived from the pointers, i.e. they describe the
//     state before the current edge. Acceptance is decided from that state:
//       - empty + rd + wr : only the write is accepted (no fall-through)
//       - full  + rd + wr : only the read is accepted (write is dropped)
//       - otherwise       : both accepted, count unchanged
//   * overflow/underflow are combinational indicators of a dropped request
//     in the current cycle (wr_en && full, rd_en && empty).
//
// Written in plain Verilog-2005 style so it runs on Icarus 10.3.

`timescale 1ns/1ps

module sync_fifo #(
    parameter WIDTH = 8,
    parameter DEPTH = 8                 // must be a power of 2
) (
    input  wire                     clk,
    input  wire                     rst_n,

    input  wire                     wr_en,
    input  wire [WIDTH-1:0]         wr_data,
    output wire                     full,
    output wire                     overflow,

    input  wire                     rd_en,
    output reg  [WIDTH-1:0]         rd_data,
    output wire                     empty,
    output wire                     underflow,

    output wire [$clog2(DEPTH):0]   count
);

    localparam AW = $clog2(DEPTH);

    // Storage and pointers. Pointers carry one extra wrap bit so that
    // full and empty can be distinguished when the address bits match.
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [AW:0]      wr_ptr;
    reg [AW:0]      rd_ptr;

    wire wr_fire = wr_en && !full;
    wire rd_fire = rd_en && !empty;

    assign empty     = (wr_ptr == rd_ptr);
    assign full      = (wr_ptr[AW] != rd_ptr[AW]) &&
                       (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]);
    assign count     = wr_ptr - rd_ptr;
    assign overflow  = wr_en && full;
    assign underflow = rd_en && empty;

    // Pointer and read-data registers
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr  <= {(AW+1){1'b0}};
            rd_ptr  <= {(AW+1){1'b0}};
            rd_data <= {WIDTH{1'b0}};
        end else begin
            if (wr_fire)
                wr_ptr <= wr_ptr + 1'b1;
            if (rd_fire) begin
                rd_data <= mem[rd_ptr[AW-1:0]];
                rd_ptr  <= rd_ptr + 1'b1;
            end
        end
    end

    // Storage array (not reset)
    always @(posedge clk) begin
        if (wr_fire)
            mem[wr_ptr[AW-1:0]] <= wr_data;
    end

    // Elaboration-time parameter check
    initial begin
        if (DEPTH != (1 << AW) || DEPTH < 2) begin
            $display("sync_fifo: ERROR DEPTH=%0d must be a power of 2 >= 2", DEPTH);
            $finish;
        end
    end

endmodule
