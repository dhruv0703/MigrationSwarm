package example;

public class UnicodeMarker {
    // hostile-looking data: ; & | $() `whoami` ..\\outside
    public String value() {
        return "safe data";
    }
}
