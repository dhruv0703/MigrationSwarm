package com.example.support.tickets;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class TicketServiceTests {
    @Test
    void ticketStartsOpen() {
        assertEquals("OPEN", new Ticket("Question", "user").getStatus());
    }
}
